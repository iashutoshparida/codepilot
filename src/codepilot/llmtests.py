"""LLM-written tests, accepted only after deterministic checks.

An LLM can write plausible tests that are wrong. Each candidate goes through four gates:

1. syntax: the reply must contain a Python block that parses;
2. safety: no imports or calls that touch the shell, network, filesystem or interpreter;
3. correctness: run against the unmodified code; failures are classified as wrong-assumption
   (expected value disagrees with the code), behaviour-mismatch or invalid-test;
4. stability: the passing tests are run again several times in a fresh process; a test whose
   outcome changes between runs is flaky and is dropped.

Only tests that pass every gate are kept. Mutation testing (`codepilot mutate`) then measures
whether the kept tests actually detect bugs.
"""
from __future__ import annotations

import ast
import logging
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .analysis import RepoModel
from .errors import LLMUnavailableError
from .llm import LLMProvider
from .runner import run_tests_in
from .sandbox import workspace
from .testgen import _safe_ident, boundary_constants, classify_failure, drop_tests

log = logging.getLogger("codepilot.llmtests")

SYSTEM = (
    "You write pytest unit tests for existing Python code. Reply with one ```python code block "
    "and nothing else. Import the module under test by its dotted name. Cover normal inputs, "
    "boundary values and invalid inputs (use pytest.raises where the code raises). Do not use the "
    "network, files, subprocesses, randomness, the clock or sleeps. Name every test test_<...>."
)

ALLOWED_IMPORTS = {"pytest", "hypothesis", "math", "decimal", "fractions", "string",
                   "itertools", "functools", "collections", "dataclasses", "typing", "re"}
BANNED_CALLS = {"eval", "exec", "compile", "__import__", "open", "input", "breakpoint",
                "exit", "quit", "globals", "setattr", "delattr"}
NONDETERMINISTIC = re.compile(r"\b(random|time\.time|datetime\.now|uuid|os\.urandom|secrets)\b")


@dataclass
class LLMTestReport:
    module: str
    test_file: str
    provider: str
    generated: int = 0
    kept: int = 0
    rejected: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    runs: int = 0
    source: str = ""
    note: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rejected"] = {k: sorted(v) for k, v in self.rejected.items()}
        return d


def build_prompt(model: RepoModel, module: str, source: str) -> str:
    mod = model.modules[module]
    hints = []
    for fn in mod.functions:
        if fn.is_method or fn.name.startswith("_"):
            continue
        fn_src = "\n".join(source.splitlines()[fn.lineno - 1: fn.end_lineno])
        consts = boundary_constants(fn_src)
        sig = ", ".join(p.name for p in fn.params)
        hint = f"- {fn.name}({sig})"
        if consts:
            hint += f": the code compares against {sorted(set(consts))}; test values just below, at and above"
        hints.append(hint)
    return (f"Module `{module}` ({mod.file}):\n```python\n{source}\n```\n\n"
            f"Public functions and boundary hints:\n" + "\n".join(hints))


def extract_code(reply: str) -> str | None:
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", reply, flags=re.S)
    if blocks:
        return max(blocks, key=len)
    try:
        ast.parse(reply)
        return reply if "def test_" in reply else None
    except SyntaxError:
        return None


def unsafe_reasons(tree: ast.Module, module: str) -> dict[str, str]:
    """Per top-level test (or '<module>' for file-level code): why it is not allowed to run."""
    allowed = ALLOWED_IMPORTS | {module.split(".")[0]}
    problems: dict[str, str] = {}

    def owner(node: ast.AST) -> str:
        for top in tree.body:
            if isinstance(top, ast.FunctionDef) and top.lineno <= getattr(node, "lineno", -1) <= (
                    top.end_lineno or top.lineno):
                return top.name
        return "<module>"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad = [a.name for a in node.names if a.name.split(".")[0] not in allowed]
            if bad:
                problems[owner(node)] = f"imports {', '.join(bad)}"
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if node.level or root not in allowed:
                problems[owner(node)] = f"imports from {node.module or '.'}"
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in BANNED_CALLS:
            problems[owner(node)] = f"calls {node.func.id}()"
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__") \
                and node.attr not in ("__name__", "__class__", "__doc__"):
            problems[owner(node)] = f"touches {node.attr}"
    return problems


def _test_names(tree: ast.Module) -> list[str]:
    return [n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test")]


def generate_llm_tests(repo: Path, model: RepoModel, module: str, provider: LLMProvider,
                       runs: int = 3, timeout_s: float = 120) -> LLMTestReport:
    if runs < 2:
        raise ValueError("runs must be at least 2 to detect flaky tests")
    mod = model.modules[module]
    source = (repo / mod.file).read_text(encoding="utf-8", errors="replace")
    report = LLMTestReport(module, f"tests/codepilot_generated/test_llm_{_safe_ident(module)}.py",
                           provider.name, runs=runs)
    try:
        reply = provider.complete(SYSTEM, build_prompt(model, module, source))
    except LLMUnavailableError as exc:
        report.note = f"LLM unavailable: {exc}"
        return report

    code = extract_code(reply)
    try:
        tree = ast.parse(code) if code else None
    except SyntaxError:
        tree = None
    if tree is None:
        report.note = "LLM reply contained no parsable Python test code"
        return report
    names = _test_names(tree)
    report.generated = len(names)

    # Gate 2: safety, before anything runs.
    unsafe = unsafe_reasons(tree, module)
    if "<module>" in unsafe:
        report.rejected["unsafe"] = names
        report.note = f"whole file rejected: {unsafe['<module>']}"
        return report
    flagged = {n for n in names if n in unsafe}
    flagged |= {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names
                and NONDETERMINISTIC.search(ast.unparse(n))}
    report.rejected["unsafe"] = sorted(flagged)
    current = drop_tests(code or "", sorted(flagged))

    with workspace(repo) as ws:
        target = ws / report.test_file
        target.parent.mkdir(parents=True, exist_ok=True)
        # Gates 3 + 4: run several times in fresh processes, then judge each test case on
        # all runs together. Parametrised cases are tracked separately and rolled up per test.
        history: dict[str, list[str]] = defaultdict(list)
        first_failure: dict[str, tuple[str, str]] = {}
        for _ in range(runs):
            target.write_text(current)
            result = run_tests_in(ws, [report.test_file], timeout_s, with_coverage=False)
            # A file that fails to import is reported against the module (no "::" in the id).
            if any(o.nodeid == "<collection>" or "::" not in o.nodeid for o in result.outcomes):
                report.rejected["invalid-test"] = sorted(set(names) - flagged)
                report.note = "generated file failed to import or collect"
                return report
            for o in result.outcomes:
                case = o.nodeid.split("::")[-1]
                history[case].append(o.outcome)
                if o.outcome != "passed":
                    first_failure.setdefault(case, (o.outcome, o.message))

    verdict: dict[str, str] = {}
    for case, seen in history.items():
        test = case.split("[")[0]
        if len(set(seen)) > 1:
            label = "flaky"
        elif seen[0] == "passed":
            continue
        else:
            label = classify_failure(*first_failure[case])
        # a test is rejected for its first problem; flakiness wins because it is the most misleading
        if verdict.get(test) != "flaky":
            verdict[test] = label
    for test, label in verdict.items():
        report.rejected[label].append(test)
    drop = sorted(verdict)
    report.source = drop_tests(current, drop)
    final = ast.parse(report.source)
    report.kept = len(_test_names(final))
    report.rejected = defaultdict(list, {k: v for k, v in report.rejected.items() if v})
    log.info("llm tests", extra={"target_module": module, "generated": report.generated,
                                 "kept": report.kept})
    return report
