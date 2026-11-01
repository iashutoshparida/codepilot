"""Test the code a pull request changes.

1. `git diff --name-only base...head` -> changed Python source files (tests and __init__ skipped).
2. Generate and validate tests for each changed module (deterministic generator, optionally
   plus LLM-written tests that pass the same gates).
3. Mutation-test only the changed files, with the existing suite and with the new tests added,
   so the report shows whether the PR's code is protected by tests that catch bugs.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .analysis import analyse_repository, module_name_for
from .errors import CodePilotError
from .ingestion import scan
from .llm import LLMProvider
from .mutation import run_mutation
from .testgen import generate_for_module

_REF_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-~^@")


@dataclass
class ModuleResult:
    module: str
    file: str
    generated: int = 0
    kept: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    edge_cases: list[str] = field(default_factory=list)
    llm_kept: int = 0
    llm_note: str | None = None


@dataclass
class PRTestReport:
    base: str
    changed_files: list[str]
    modules: list[ModuleResult]
    mutation_before: float | None
    mutation_after: float | None
    mutants: int
    test_files: dict[str, str] = field(default_factory=dict)
    note: str | None = None

    def to_markdown(self) -> str:
        out = [f"## CodePilot: tests for code changed since `{self.base}`", ""]
        if not self.modules:
            return "\n".join(out + [self.note or "No changed Python source files.", ""])
        if self.mutation_before is not None:
            out.append(f"Mutation score on changed files: **{self.mutation_before:.1f}%** with the "
                       f"existing tests -> **{self.mutation_after:.1f}%** with the generated tests "
                       f"({self.mutants} mutants).")
            out.append("")
        out += ["| Module | Generated | Kept | Dropped | LLM tests kept |", "|---|---|---|---|---|"]
        for m in self.modules:
            dropped = ", ".join(f"{k} {v}" for k, v in m.dropped.items()) or "-"
            out.append(f"| `{m.module}` | {m.generated} | {m.kept} | {dropped} | {m.llm_kept} |")
        findings = [e for m in self.modules for e in m.edge_cases]
        if findings:
            out += ["", "### Inputs that crash the changed code (review these)", ""]
            out += [f"- {e}" for e in findings[:20]]
        if self.note:
            out += ["", self.note]
        return "\n".join(out) + "\n"


def changed_python_files(repo: Path, base: str, head: str = "HEAD") -> list[str]:
    for ref in (base, head):
        if not ref or ref.startswith("-") or not set(ref) <= _REF_CHARS:
            raise CodePilotError(f"invalid git ref: {ref!r}")
    proc = subprocess.run(  # noqa: S603 - argv list, validated refs
        ["git", "-C", str(repo), "diff", "--name-only", "--diff-filter=AMR", f"{base}...{head}"],  # noqa: S607
        capture_output=True, text=True, timeout=60, check=False)
    if proc.returncode != 0:
        raise CodePilotError(proc.stderr.strip() or "git diff failed")
    files = []
    for path in proc.stdout.splitlines():
        name = path.rsplit("/", 1)[-1]
        if path.endswith(".py") and not name.startswith("test_") and name != "__init__.py" \
                and "/tests/" not in f"/{path}" and name != "conftest.py":
            files.append(path)
    return files


def analyse_pr_tests(repo: Path, base: str, existing_tests: list[str], head: str = "HEAD",
                      llm: LLMProvider | None = None, max_mutants: int | None = 150,
                      timeout_s: float = 120) -> PRTestReport:
    files = changed_python_files(repo, base, head)
    report = PRTestReport(base, files, [], None, None, 0)
    if not files:
        report.note = "No changed Python source files."
        return report
    model = analyse_repository(repo, scan(repo))
    by_file = {m.file: m.name for m in model.modules.values()}
    for path in files:
        module = by_file.get(path) or module_name_for(path)
        if module not in model.modules or not model.modules[module].functions:
            continue
        gen = generate_for_module(repo, model, module, timeout_s=timeout_s)
        result = ModuleResult(module, path, gen.generated_tests, gen.kept_tests)
        for d in gen.dropped_tests:
            result.dropped[d["reason"]] = result.dropped.get(d["reason"], 0) + 1
        result.edge_cases = [f"`{module}:{e.function}({e.args})` raises {e.exception}"
                             for e in gen.edge_case_findings]
        if gen.source and gen.kept_tests:
            report.test_files[gen.test_file] = gen.source
        if llm is not None:
            from .llmtests import generate_llm_tests

            lt = generate_llm_tests(repo, model, module, llm, timeout_s=timeout_s)
            result.llm_kept, result.llm_note = lt.kept, lt.note
            if lt.kept:
                report.test_files[lt.test_file] = lt.source
        report.modules.append(result)

    if report.modules:
        try:
            before = run_mutation(repo, files, existing_tests, max_mutants=max_mutants)
            after = run_mutation(repo, files, existing_tests + sorted(report.test_files),
                                 extra_files=report.test_files, max_mutants=max_mutants)
            report.mutation_before, report.mutation_after = before.score, after.score
            report.mutants = after.total
        except RuntimeError as exc:  # existing suite is red: mutation testing is meaningless
            report.note = f"Mutation testing skipped: {exc}"
    return report
