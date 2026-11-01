"""Module 4: deterministic maintainability and safety checks.

Every finding comes from the AST or the call graph, with a file and line number.
No finding is produced by an LLM.
"""
from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from .analysis import RepoModel
from .graphs import Graphs


@dataclass(frozen=True)
class Thresholds:
    max_function_lines: int = 50
    max_complexity: int = 10
    max_nesting: int = 4
    max_params: int = 6
    max_fan_out: int = 8
    min_duplicate_lines: int = 4


@dataclass
class Finding:
    rule: str
    severity: str        # info | warning | error
    file: str
    line: int
    symbol: str | None
    message: str


DANGEROUS_CALLS = {
    "eval": "eval() runs arbitrary code",
    "exec": "exec() runs arbitrary code",
    "os.system": "os.system() passes a string to the shell",
    "pickle.loads": "pickle.loads() can run code from untrusted data",
    "pickle.load": "pickle.load() can run code from untrusted data",
    "yaml.load": "yaml.load() without SafeLoader can build arbitrary objects",
    "subprocess.call": "check that shell=True is not used with user input",
    "subprocess.run": "check that shell=True is not used with user input",
    "subprocess.Popen": "check that shell=True is not used with user input",
}
RISKY_IO = {"open", "requests.get", "requests.post", "urllib.request.urlopen", "json.load", "json.loads"}


MUTABLE_FACTORIES = {"list", "dict", "set"}


def _is_mutable_default(d: ast.expr) -> bool:
    if isinstance(d, (ast.List, ast.Dict, ast.Set)):
        return True
    return isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id in MUTABLE_FACTORIES


class _SyntaxRules(ast.NodeVisitor):
    def __init__(self, module: str, file: str, out: list[Finding]):
        self.module, self.file, self.out = module, file, out
        self.scope: list[str] = []
        self.in_function = 0

    def _symbol(self) -> str | None:
        return f"{self.module}:{'.'.join(self.scope)}" if self.in_function else None

    def _add(self, rule: str, sev: str, node: ast.AST, msg: str) -> None:
        self.out.append(Finding(rule, sev, self.file, getattr(node, "lineno", 0), self._symbol(), msg))

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.in_function += 1
        for d in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
            if _is_mutable_default(d):
                self._add("mutable-default", "warning", d,
                          f"'{node.name}' has a mutable default argument shared across calls")
        self.generic_visit(node)
        self.in_function -= 1
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        broad = isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"}
        if node.type is None:
            self._add("bare-except", "warning", node,
                      "bare 'except:' also catches KeyboardInterrupt and SystemExit")
        elif broad and all(isinstance(s, (ast.Pass, ast.Continue)) for s in node.body):
            self._add("swallowed-exception", "warning", node,
                      f"'except {ast.unparse(node.type)}' silently ignores errors")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = ast.unparse(node.func)
        if name in DANGEROUS_CALLS:
            shell = any(k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True
                        for k in node.keywords)
            if not name.startswith("subprocess") or shell:
                suffix = " (shell=True)" if shell else ""
                self._add("unsafe-call", "error", node, DANGEROUS_CALLS[name] + suffix)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        for op, right in zip(node.ops, node.comparators, strict=True):
            is_none = isinstance(right, ast.Constant) and right.value is None
            if isinstance(op, (ast.Eq, ast.NotEq)) and is_none:
                self._add("none-comparison", "info", node, "compare with None using 'is' / 'is not'")
        self.generic_visit(node)


def _unused_imports(model: RepoModel) -> list[Finding]:
    out = []
    for mod in model.modules.values():
        if mod.parse_error or mod.file.endswith("__init__.py"):
            continue  # __init__ files often re-export names on purpose
        for imp in mod.imports:
            if imp.name == "*" or imp.alias in mod.used_names or imp.alias in mod.exported:
                continue
            if imp.module == "__future__":
                continue
            out.append(Finding("unused-import", "info", mod.file, imp.lineno, None,
                               f"'{imp.alias}' is imported but never used"))
    return out


def analyse_quality(root: Path, model: RepoModel, graphs: Graphs | None = None,
                    t: Thresholds | None = None) -> list[Finding]:
    t = t or Thresholds()
    out: list[Finding] = []
    by_hash: dict[str, list] = defaultdict(list)

    for mod in model.modules.values():
        if mod.parse_error:
            out.append(Finding("parse-error", "error", mod.file, 0, None, mod.parse_error))
            continue
        for fn in mod.functions:
            sym = fn.qualname
            if fn.length > t.max_function_lines:
                out.append(Finding("long-function", "warning", fn.file, fn.lineno, sym,
                                   f"{fn.length} lines (limit {t.max_function_lines})"))
            if fn.complexity > t.max_complexity:
                out.append(Finding("high-complexity", "warning", fn.file, fn.lineno, sym,
                                   f"cyclomatic complexity {fn.complexity} (limit {t.max_complexity})"))
            if fn.max_nesting > t.max_nesting:
                out.append(Finding("deep-nesting", "warning", fn.file, fn.lineno, sym,
                                   f"nesting depth {fn.max_nesting} (limit {t.max_nesting})"))
            n_params = len([p for p in fn.params if p.name not in ("self", "cls")])
            if n_params > t.max_params:
                out.append(Finding("too-many-params", "info", fn.file, fn.lineno, sym,
                                   f"{n_params} parameters (limit {t.max_params})"))
            io_calls = sorted(set(fn.raw_calls) & RISKY_IO)
            if io_calls and not fn.has_try:
                out.append(Finding("unhandled-io", "info", fn.file, fn.lineno, sym,
                                   f"calls {', '.join(io_calls)} with no try/except in this function"))
            if fn.length >= t.min_duplicate_lines:
                by_hash[fn.body_hash].append(fn)
        tree = ast.parse((root / mod.file).read_text(encoding="utf-8", errors="replace"))
        _SyntaxRules(mod.name, mod.file, out).visit(tree)

    for group in by_hash.values():
        if len(group) > 1:
            names = ", ".join(f.qualname for f in group)
            for fn in group:
                out.append(Finding("duplicate-code", "warning", fn.file, fn.lineno, fn.qualname,
                                   f"same body as: {names}"))

    out.extend(_unused_imports(model))
    if graphs is not None:
        for qual, n in graphs.fan_out().items():
            if n > t.max_fan_out:
                f = model.functions()[qual]
                out.append(Finding("high-coupling", "info", f.file, f.lineno, qual,
                                   f"calls {n} other functions in this repository (limit {t.max_fan_out})"))
        for cycle in graphs.module_cycles():
            first = model.modules[cycle[0]]
            out.append(Finding("import-cycle", "warning", first.file, 0, None,
                               "circular import: " + " -> ".join(cycle + [cycle[0]])))
    order = {"error": 0, "warning": 1, "info": 2}
    return sorted(out, key=lambda f: (order[f.severity], f.file, f.line, f.rule))


def summarise(findings: list[Finding]) -> dict:
    by_rule: dict[str, int] = defaultdict(int)
    by_sev: dict[str, int] = defaultdict(int)
    for f in findings:
        by_rule[f.rule] += 1
        by_sev[f.severity] += 1
    return {"total": len(findings), "by_severity": dict(by_sev), "by_rule": dict(by_rule),
            "findings": [asdict(f) for f in findings]}
