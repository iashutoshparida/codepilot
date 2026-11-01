"""Module 5b: AI-assisted code understanding, grounded in the deterministic analysis.

The model only sees source and facts that CodePilot extracted itself. Every answer is
checked afterwards: file:line citations must point at real lines, and function names the
answer mentions must exist. Patches are returned as proposals and never applied here.
"""
from __future__ import annotations

import ast
import builtins
import difflib
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

from .analysis import FunctionInfo, RepoModel
from .errors import SymbolNotFoundError
from .graphs import Graphs
from .llm import LLMProvider
from .quality import Finding

SYSTEM = (
    "You are a careful senior engineer reviewing a Python codebase. Use only the context given. "
    "Cite source lines as [path:line] or [path:start-end]. If the context is not enough, say so. "
    "Never invent functions, files or behaviour."
)
CITATION = re.compile(r"\[([\w./-]+\.py):(\d+)(?:-(\d+))?\]")
MENTION = re.compile(r"`([A-Za-z_][\w.]*)\(\)?`")


@dataclass
class GroundedAnswer:
    answer: str
    references: list[str]
    valid_citations: list[str] = field(default_factory=list)
    invalid_citations: list[str] = field(default_factory=list)
    unknown_mentions: list[str] = field(default_factory=list)

    @property
    def grounded(self) -> bool:
        return not self.invalid_citations and not self.unknown_mentions


@dataclass
class PatchProposal:
    qualname: str
    file: str
    start_line: int
    end_line: int
    original: str
    replacement: str
    diff: str
    warnings: list[str]


class CodeAssistant:
    def __init__(self, root: Path, model: RepoModel, graphs: Graphs, provider: LLMProvider,
                 findings: list[Finding] | None = None):
        self.root, self.model, self.graphs, self.provider = root, model, graphs, provider
        self.findings = findings or []
        self._line_counts: dict[str, int] = {}

    # ------------------------------------------------------------- context
    def function(self, qualname: str) -> FunctionInfo:
        fn = self.model.functions().get(qualname)
        if fn is None:
            raise SymbolNotFoundError(f"Unknown function: {qualname}")
        return fn

    def source_of(self, fn: FunctionInfo, numbered: bool = True) -> str:
        lines = (self.root / fn.file).read_text(encoding="utf-8").splitlines()[fn.lineno - 1: fn.end_lineno]
        if not numbered:
            return "\n".join(lines)
        return "\n".join(f"{fn.lineno + i:>4} | {line}" for i, line in enumerate(lines))

    def context(self, qualname: str) -> str:
        fn = self.function(qualname)
        issues = [f"- {f.rule} (line {f.line}): {f.message}" for f in self.findings if f.symbol == qualname]
        parts = [
            f"Function {qualname} in {fn.file} lines {fn.lineno}-{fn.end_lineno}:",
            self.source_of(fn),
            f"Called by: {', '.join(self.graphs.callers(qualname)) or 'nothing in this repository'}",
            f"Calls: {', '.join(self.graphs.callees(qualname)) or 'no repository functions'}",
            f"External calls: {', '.join(self.graphs.external_calls.get(qualname, [])) or 'none'}",
            "Static analysis findings:\n" + ("\n".join(issues) if issues else "- none"),
        ]
        for callee in self.graphs.callees(qualname)[:5]:
            c = self.function(callee)
            parts.append(f"Source of callee {callee} ({c.file}):\n{self.source_of(c)}")
        return "\n\n".join(parts)

    # ------------------------------------------------------------- explain
    def explain(self, qualname: str, question: str | None = None) -> GroundedAnswer:
        fn = self.function(qualname)
        q = question or "Explain what this function does, its inputs and outputs, edge cases, and what depends on it."
        answer = self.provider.complete(SYSTEM, f"{self.context(qualname)}\n\nQuestion: {q}")
        ref = f"{fn.file}:{fn.lineno}-{fn.end_lineno}"
        return self.verify(answer, [ref])

    def verify(self, answer: str, references: list[str]) -> GroundedAnswer:
        out = GroundedAnswer(answer=answer, references=references)
        for m in CITATION.finditer(answer):
            path, start = m.group(1), int(m.group(2))
            end = int(m.group(3) or start)
            n = self._lines_in(path)
            ok = n is not None and 1 <= start <= end <= n
            (out.valid_citations if ok else out.invalid_citations).append(m.group(0))
        known = self._known_names()
        for name in {m.group(1) for m in MENTION.finditer(answer)}:
            if name.split(".")[-1] not in known:
                out.unknown_mentions.append(name)
        out.unknown_mentions.sort()
        return out

    def _lines_in(self, rel: str) -> int | None:
        if rel not in self._line_counts:
            p = (self.root / rel).resolve()
            if self.root.resolve() not in p.parents or not p.is_file():
                return None
            self._line_counts[rel] = len(p.read_text(encoding="utf-8").splitlines())
        return self._line_counts[rel]

    def _known_names(self) -> set[str]:
        names = set(dir(builtins))
        for fn in self.model.functions().values():
            names.add(fn.name)
            names.update(p.name for p in fn.params)
            names.update(c.split(".")[-1] for c in fn.raw_calls)
        names.update(c.name for c in self.model.classes().values())
        names.update(i.alias for m in self.model.modules.values() for i in m.imports)
        names.update(self.model.modules)
        return names

    # ------------------------------------------------------------- patches
    def propose_patch(self, qualname: str, instruction: str) -> PatchProposal:
        fn = self.function(qualname)
        prompt = (f"{self.context(qualname)}\n\nTask: {instruction}\n"
                  f"Return only the complete new version of `{fn.name}` in one ```python block. "
                  "Keep the same name and parameters unless the task says otherwise.")
        reply = self.provider.complete(SYSTEM, prompt)
        m = re.search(r"```(?:python)?\n(.*?)```", reply, re.S)
        code = m.group(1) if m else reply
        return build_proposal(self.root, fn, code)


def build_proposal(root: Path, fn: FunctionInfo, new_code: str) -> PatchProposal:
    """Validate a replacement for `fn` and return a reviewable diff. Nothing is written."""
    from .errors import PatchRejectedError
    code = textwrap.dedent(new_code).strip("\n") + "\n"
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise PatchRejectedError(f"proposed code does not parse: line {exc.lineno}: {exc.msg}") from exc
    defs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(defs) != 1 or defs[0].name != fn.name:
        raise PatchRejectedError(f"proposed code must define exactly one function named '{fn.name}'")
    warnings = []
    new_params = [a.arg for a in defs[0].args.posonlyargs + defs[0].args.args + defs[0].args.kwonlyargs]
    old_params = [p.name for p in fn.params if p.kind in ("positional", "kwonly")]
    if new_params != old_params:
        warnings.append(f"signature changed: ({', '.join(old_params)}) -> ({', '.join(new_params)}); callers may break")
    lines = (root / fn.file).read_text(encoding="utf-8").splitlines(keepends=True)
    original = "".join(lines[fn.lineno - 1: fn.end_lineno])
    indent = re.match(r"\s*", lines[fn.lineno - 1]).group(0)  # type: ignore[union-attr]
    replacement = textwrap.indent(code, indent)
    diff = "".join(difflib.unified_diff(original.splitlines(keepends=True), replacement.splitlines(keepends=True),
                                        fromfile=f"a/{fn.file}", tofile=f"b/{fn.file}", n=2))
    if not diff:
        warnings.append("proposed code is identical to the current code")
    return PatchProposal(fn.qualname, fn.file, fn.lineno, fn.end_lineno, original, replacement, diff, warnings)
