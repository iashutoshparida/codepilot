"""Module 7: measure whether a test suite catches behaviour changes.

Two methods:
* Mutation testing: make one small, deliberate change to the code (a "mutant"), run the
  tests, and see whether any test fails ("killed"). Survivors show behaviour the tests
  do not check.
* Bug injection: apply a list of known, realistic bugs one at a time and count how many
  the suite detects.
"""
from __future__ import annotations

import ast
import logging
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .runner import run_tests_in
from .sandbox import workspace

log = logging.getLogger("codepilot.mutation")

SWAP_BINOP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult}
SWAP_CMP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Eq: ast.NotEq,
            ast.NotEq: ast.Eq, ast.In: ast.NotIn, ast.NotIn: ast.In}
SWAP_BOOL = {ast.And: ast.Or, ast.Or: ast.And}


@dataclass
class Mutant:
    id: int
    file: str
    line: int
    operator: str
    description: str


@dataclass
class MutantResult:
    mutant: Mutant
    status: str      # killed | survived | timeout | invalid


@dataclass
class MutationReport:
    total: int = 0
    killed: int = 0
    survived: int = 0
    timeout: int = 0
    invalid: int = 0
    results: list[MutantResult] = field(default_factory=list)

    @property
    def score(self) -> float:
        valid = self.total - self.invalid
        return round((self.killed + self.timeout) / valid * 100, 2) if valid else 0.0

    def survivors(self) -> list[Mutant]:
        return [r.mutant for r in self.results if r.status == "survived"]

    def to_dict(self) -> dict:
        return {"total": self.total, "killed": self.killed, "survived": self.survived, "timeout": self.timeout,
                "invalid": self.invalid, "score": self.score,
                "survivors": [asdict(m) for m in self.survivors()],
                "by_operator": self.by_operator()}

    def by_operator(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in self.results:
            d = out.setdefault(r.mutant.operator, {"total": 0, "killed": 0})
            d["total"] += 1
            d["killed"] += r.status in ("killed", "timeout")
        return out


def _in_main_guard(node: ast.If) -> bool:
    t = node.test
    return (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name) and t.left.id == "__name__")


class _Mutator(ast.NodeTransformer):
    """Walks the tree; when `target` is None it only records sites, else it mutates site `target`."""

    def __init__(self, target: int | None = None):
        self.target = target
        self.sites: list[tuple[int, str, str]] = []   # (line, operator, description)
        self.done = False

    def _site(self, node: ast.AST, op: str, desc: str) -> bool:
        idx = len(self.sites)
        self.sites.append((getattr(node, "lineno", 0), op, desc))
        return idx == self.target

    def visit_If(self, node: ast.If):
        if _in_main_guard(node):
            return node
        return self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr):
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node  # docstrings
        return self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp):
        self.generic_visit(node)
        new = SWAP_BINOP.get(type(node.op))
        if new and self._site(node, "arithmetic", f"{type(node.op).__name__} -> {new.__name__}"):
            node.op = new()
        return node

    def visit_AugAssign(self, node: ast.AugAssign):
        self.generic_visit(node)
        new = SWAP_BINOP.get(type(node.op))
        if new and self._site(node, "arithmetic", f"{type(node.op).__name__}= -> {new.__name__}="):
            node.op = new()
        return node

    def visit_Compare(self, node: ast.Compare):
        self.generic_visit(node)
        for i, op in enumerate(node.ops):
            new = SWAP_CMP.get(type(op))
            if new and self._site(node, "comparison", f"{type(op).__name__} -> {new.__name__}"):
                node.ops[i] = new()
        return node

    def visit_BoolOp(self, node: ast.BoolOp):
        self.generic_visit(node)
        new = SWAP_BOOL[type(node.op)]
        if self._site(node, "logical", f"{type(node.op).__name__} -> {new.__name__}"):
            node.op = new()
        return node

    def visit_UnaryOp(self, node: ast.UnaryOp):
        self.generic_visit(node)
        if isinstance(node.op, ast.Not) and self._site(node, "negation", "remove 'not'"):
            return node.operand
        return node

    def visit_Constant(self, node: ast.Constant):
        v = node.value
        if isinstance(v, int) and not isinstance(v, bool) and self._site(node, "constant", f"{v} -> {v + 1}"):
            return ast.copy_location(ast.Constant(v + 1), node)
        return node

    def visit_Return(self, node: ast.Return):
        self.generic_visit(node)
        is_none = node.value is None or (isinstance(node.value, ast.Constant) and node.value.value is None)
        if not is_none and self._site(node, "return", "return value -> None"):
            node.value = ast.Constant(None)
        return node


def discover(source: str, file: str) -> list[Mutant]:
    m = _Mutator()
    m.visit(ast.parse(source))
    return [Mutant(i, file, line, op, desc) for i, (line, op, desc) in enumerate(m.sites)]


def apply(source: str, site: int) -> str:
    tree = ast.parse(source)
    m = _Mutator(target=site)
    tree = m.visit(tree)
    return ast.unparse(ast.fix_missing_locations(tree))


def run_mutation(repo: Path, source_files: list[str], test_paths: list[str],
                 extra_files: dict[str, str] | None = None, max_mutants: int | None = 200,
                 seed: int = 0, timeout_s: float = 60) -> MutationReport:
    """Run each mutant against the suite. `source_files` are repo-relative paths to mutate."""
    report = MutationReport()
    with workspace(repo) as ws:
        for rel, text in (extra_files or {}).items():
            p = ws / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        baseline = run_tests_in(ws, test_paths, timeout_s, with_coverage=False)
        if not baseline.green:
            raise RuntimeError("the test suite must pass on the original code before mutation testing")
        mutants: list[tuple[str, Mutant]] = []
        for rel in source_files:
            src = (ws / rel).read_text()
            mutants += [(src, m) for m in discover(src, rel)]
        if max_mutants is not None and len(mutants) > max_mutants:
            mutants = sorted(random.Random(seed).sample(mutants, max_mutants), key=lambda x: (x[1].file, x[1].id))
        for original, mutant in mutants:
            path = ws / mutant.file
            try:
                path.write_text(apply(original, mutant.id))
                r = run_tests_in(ws, test_paths, timeout_s, with_coverage=False, fail_fast=True)
                if r.timed_out:
                    status = "timeout"
                elif r.outcomes and r.outcomes[0].nodeid == "<collection>":
                    status = "invalid"
                elif r.failed or r.errors:
                    status = "killed"
                else:
                    status = "survived"
            finally:
                path.write_text(original)
            report.results.append(MutantResult(mutant, status))
    for res in report.results:
        setattr(report, res.status, getattr(report, res.status) + 1)
    report.total = len(report.results)
    log.info("mutation run", extra={"total": report.total, "score": report.score})
    return report


@dataclass
class InjectedBug:
    id: str
    file: str
    find: str
    replace: str
    description: str


def run_bug_injection(repo: Path, bugs: list[InjectedBug], test_paths: list[str],
                      extra_files: dict[str, str] | None = None, timeout_s: float = 60) -> dict:
    detected, rows = 0, []
    with workspace(repo) as ws:
        for rel, text in (extra_files or {}).items():
            p = ws / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        for bug in bugs:
            path = ws / bug.file
            original = path.read_text()
            if original.count(bug.find) != 1:
                raise ValueError(f"bug {bug.id}: pattern must match exactly once in {bug.file}")
            try:
                path.write_text(original.replace(bug.find, bug.replace))
                r = run_tests_in(ws, test_paths, timeout_s, with_coverage=False, fail_fast=True)
                caught = bool(r.failed or r.errors or r.timed_out)
            finally:
                path.write_text(original)
            detected += caught
            rows.append({"id": bug.id, "description": bug.description, "detected": caught})
    return {"total": len(bugs), "detected": detected,
            "detection_rate": round(detected / len(bugs) * 100, 2) if bugs else 0.0, "bugs": rows}
