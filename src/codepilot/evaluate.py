"""End-to-end evaluation: does adding CodePilot's generated tests make a suite catch more bugs?

Compares the repository's own tests ("baseline") with baseline + generated tests on:
line and branch coverage, mutation score, and detection of known injected bugs.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from .analysis import analyse_repository
from .graphs import build_graphs
from .ingestion import scan
from .mutation import InjectedBug, run_bug_injection, run_mutation
from .quality import analyse_quality, summarise
from .runner import run_tests
from .testgen import generate_for_module


def evaluate(repo: Path, baseline_tests: list[str], bugs_file: Path | None = None,
             max_mutants: int | None = None) -> dict:
    t0 = time.time()
    inv = scan(repo)
    model = analyse_repository(repo, inv)
    graphs = build_graphs(model)
    findings = analyse_quality(repo, model, graphs)
    sources = [f.path for f in inv.python_sources() if not f.path.endswith("__init__.py")]

    generated: dict[str, str] = {}
    gen_reports = []
    for name in sorted(model.modules):
        if name.endswith("__init__") or not model.modules[name].functions:
            continue
        res = generate_for_module(repo, model, name)
        gen_reports.append(res)
        if res.source and res.kept_tests:
            generated[res.test_file] = res.source
    gen_paths = sorted(generated)
    bugs = [InjectedBug(**b) for b in json.loads(bugs_file.read_text())] if bugs_file else []

    def suite(paths: list[str], extra: dict[str, str]) -> dict:
        run = run_tests(repo, paths, extra_files=extra)
        mut = run_mutation(repo, sources, paths, extra_files=extra, max_mutants=max_mutants)
        out = {"tests": run.total, "passed": run.passed, "line_coverage": run.line_coverage,
               "branch_coverage": run.branch_coverage, "mutation": mut.to_dict()}
        if bugs:
            out["bug_injection"] = run_bug_injection(repo, bugs, paths, extra_files=extra)
        return out

    baseline = suite(baseline_tests, {})
    combined = suite(baseline_tests + gen_paths, generated)
    return {
        "repository": repo.name,
        "inventory": {k: v for k, v in inv.to_dict().items() if k != "files"},
        "model": model.summary(),
        "quality": {k: v for k, v in summarise(findings).items() if k != "findings"},
        "quality_findings": [asdict(f) for f in findings],
        "generation": {
            "modules": len(gen_reports),
            "functions_covered": sum(len(r.functions_covered) for r in gen_reports),
            "functions_skipped": {r.module: r.functions_skipped for r in gen_reports if r.functions_skipped},
            "generated_tests": sum(r.generated_tests for r in gen_reports),
            "kept_tests": sum(r.kept_tests for r in gen_reports),
            "dropped_tests": [d | {"module": r.module} for r in gen_reports for d in r.dropped_tests],
            "edge_case_findings": [asdict(f) for r in gen_reports for f in r.edge_case_findings],
        },
        "baseline": baseline,
        "with_generated": combined,
        "generated_files": generated,
        "seconds": round(time.time() - t0, 1),
    }


def to_markdown(r: dict) -> str:
    b, c = r["baseline"], r["with_generated"]

    def pct(v):
        return "n/a" if v is None else f"{v:.1f}%"

    rows = [
        ("Tests", b["tests"], c["tests"]),
        ("Line coverage", pct(b["line_coverage"]), pct(c["line_coverage"])),
        ("Branch coverage", pct(b["branch_coverage"]), pct(c["branch_coverage"])),
        ("Mutants killed", f'{b["mutation"]["killed"]}/{b["mutation"]["total"]}',
         f'{c["mutation"]["killed"]}/{c["mutation"]["total"]}'),
        ("Mutation score", pct(b["mutation"]["score"]), pct(c["mutation"]["score"])),
    ]
    if "bug_injection" in b:
        rows.append(("Injected bugs detected", f'{b["bug_injection"]["detected"]}/{b["bug_injection"]["total"]}',
                     f'{c["bug_injection"]["detected"]}/{c["bug_injection"]["total"]}'))
    g = r["generation"]
    lines = [
        f"# Evaluation: `{r['repository']}`", "",
        "Numbers below are produced by `codepilot evaluate`; rerun it to reproduce them.", "",
        "| Metric | Original tests | Original + CodePilot tests |", "|---|---|---|",
        *[f"| {a} | {x} | {y} |" for a, x, y in rows], "",
        "## Test generation", "",
        f"- Functions with generated tests: {g['functions_covered']}",
        f"- Tests generated: {g['generated_tests']}; kept after validation: {g['kept_tests']}; "
        f"dropped: {len(g['dropped_tests'])}",
    ]
    for d in g["dropped_tests"]:
        msg = " ".join(d["message"].split())[:110]
        lines.append(f"  - dropped `{d['test']}` ({d['reason']}): {msg}")
    lines += ["", "## Edge-case findings (for human review)", ""]
    lines += [f"- `{f['function']}{f['args']}` raises `{f['exception']}`: {f['message']}"
              for f in g["edge_case_findings"]] or ["- none"]
    if "bug_injection" in c:
        lines += ["", "## Injected bugs", "", "| Bug | Description | Original | With CodePilot |", "|---|---|---|---|"]
        base = {x["id"]: x["detected"] for x in b["bug_injection"]["bugs"]}
        for x in c["bug_injection"]["bugs"]:
            lines.append(f"| {x['id']} | {x['description']} | {'caught' if base[x['id']] else 'missed'} | "
                         f"{'caught' if x['detected'] else 'missed'} |")
    survivors = c["mutation"]["survivors"]
    lines += ["", f"## Surviving mutants with generated tests ({len(survivors)})", ""]
    lines += [f"- {m['file']}:{m['line']} {m['operator']}: {m['description']}" for m in survivors[:25]] or ["- none"]
    q = r["quality"]
    lines += ["", "## Static analysis", "", f"{q['total']} findings: " +
              ", ".join(f"{k} {v}" for k, v in sorted(q["by_rule"].items())), "",
              f"Run time: {r['seconds']} s"]
    return "\n".join(lines) + "\n"
