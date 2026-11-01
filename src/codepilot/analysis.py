"""Module 2: parse Python files with the standard `ast` module into a repository model.

This only handles Python. Other languages are listed by ingestion but not parsed;
a new language would add its own parser that fills the same dataclasses.
"""
from __future__ import annotations

import ast
import hashlib
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .ingestion import Inventory

log = logging.getLogger("codepilot.analysis")

_BRANCHES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.ExceptHandler, ast.Assert)
_NESTING = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.Match)
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


@dataclass
class Param:
    name: str
    annotation: str | None
    has_default: bool
    default: str | None
    kind: str  # positional | vararg | kwonly | kwarg


@dataclass
class FunctionInfo:
    qualname: str          # "pkg.mod:Class.method" or "pkg.mod:outer.inner"
    module: str
    name: str
    file: str
    lineno: int
    end_lineno: int
    params: list[Param]
    returns: str | None
    is_method: bool
    is_async: bool
    decorators: list[str]
    docstring: str | None
    complexity: int
    max_nesting: int
    raw_calls: list[str]   # call targets as written: "helper", "self.save", "os.path.join"
    has_try: bool
    body_hash: str         # hash of the body with names kept, used to spot copy-pasted functions
    parent_class: str | None = None

    @property
    def length(self) -> int:
        return self.end_lineno - self.lineno + 1


@dataclass
class ClassInfo:
    qualname: str
    module: str
    name: str
    file: str
    lineno: int
    bases: list[str]
    methods: list[str]


@dataclass
class ImportInfo:
    module: str            # absolute module being imported, e.g. "pkg.util" or "os"
    name: str | None       # imported symbol for `from x import name`, else None
    alias: str             # local name bound by the import
    lineno: int


@dataclass
class ModuleInfo:
    name: str
    file: str
    imports: list[ImportInfo] = field(default_factory=list)
    functions: list[FunctionInfo] = field(default_factory=list)
    classes: list[ClassInfo] = field(default_factory=list)
    used_names: set[str] = field(default_factory=set)
    exported: set[str] = field(default_factory=set)   # names listed in __all__
    parse_error: str | None = None


@dataclass
class RepoModel:
    root: str
    modules: dict[str, ModuleInfo] = field(default_factory=dict)

    def functions(self) -> dict[str, FunctionInfo]:
        return {f.qualname: f for m in self.modules.values() for f in m.functions}

    def classes(self) -> dict[str, ClassInfo]:
        return {c.qualname: c for m in self.modules.values() for c in m.classes}

    def summary(self) -> dict:
        fns = self.functions()
        return {
            "modules": len(self.modules),
            "functions": len(fns),
            "classes": sum(len(m.classes) for m in self.modules.values()),
            "parse_errors": {m.file: m.parse_error for m in self.modules.values() if m.parse_error},
            "max_complexity": max((f.complexity for f in fns.values()), default=0),
        }

    def to_dict(self) -> dict:
        modules: dict[str, dict] = {}
        out = {"root": self.root, "summary": self.summary(), "modules": modules}
        for name, m in self.modules.items():
            modules[name] = {
                "file": m.file,
                "imports": [asdict(i) for i in m.imports],
                "functions": [asdict(f) | {"length": f.length} for f in m.functions],
                "classes": [asdict(c) for c in m.classes],
                "parse_error": m.parse_error,
            }
        return out


def module_name_for(rel_path: str) -> str:
    parts = list(Path(rel_path).with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or "__root__"


def _unparse(node: ast.AST | None) -> str | None:
    return ast.unparse(node) if node is not None else None


def call_target(node: ast.Call) -> str | None:
    """Return a dotted name for simple call targets, None for computed ones like f()()."""
    parts: list[str] = []
    cur: ast.AST = node.func
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


def _walk_own_body(node: ast.AST):
    """Yield nodes inside a function, but not inside nested functions, classes or lambdas."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        cur = stack.pop()
        yield cur
        if not isinstance(cur, _SCOPES):
            stack.extend(ast.iter_child_nodes(cur))


def cyclomatic_complexity(fn: ast.AST) -> int:
    score = 1
    for n in _walk_own_body(fn):
        if isinstance(n, _BRANCHES):
            score += 1
        elif isinstance(n, ast.BoolOp):
            score += len(n.values) - 1
        elif isinstance(n, ast.comprehension):
            score += 1 + len(n.ifs)
        elif isinstance(n, ast.match_case):
            score += 1
    return score


def max_nesting(fn: ast.AST) -> int:
    def depth(node: ast.AST, level: int) -> int:
        best = level
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPES):
                continue
            nxt = level + 1 if isinstance(child, _NESTING) else level
            best = max(best, depth(child, nxt))
        return best
    return depth(fn, 0)


def _params(args: ast.arguments) -> list[Param]:
    out: list[Param] = []
    positional = args.posonlyargs + args.args
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    for a, d in zip(positional, defaults, strict=True):
        out.append(Param(a.arg, _unparse(a.annotation), d is not None, _unparse(d), "positional"))
    if args.vararg:
        out.append(Param(args.vararg.arg, _unparse(args.vararg.annotation), False, None, "vararg"))
    for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        out.append(Param(a.arg, _unparse(a.annotation), d is not None, _unparse(d), "kwonly"))
    if args.kwarg:
        out.append(Param(args.kwarg.arg, _unparse(args.kwarg.annotation), False, None, "kwarg"))
    return out


def _body_hash(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    body = fn.body
    first = body[0] if body else None
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        body = body[1:]  # ignore the docstring
    dump = "".join(ast.dump(stmt, annotate_fields=False) for stmt in body)
    return hashlib.sha1(dump.encode()).hexdigest()[:16]


class _ModuleVisitor(ast.NodeVisitor):
    def __init__(self, info: ModuleInfo, package: str):
        self.info = info
        self.package = package
        self.scope: list[str] = []
        self.class_stack: list[str] = []

    # imports -------------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            alias = a.asname or a.name.split(".")[0]
            self.info.imports.append(ImportInfo(a.name, None, alias, node.lineno))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = node.module or ""
        if node.level:  # relative import: resolve against this module's package
            pkg_parts = self.package.split(".") if self.package else []
            keep = len(pkg_parts) - (node.level - 1)
            base = ".".join(pkg_parts[:max(keep, 0)] + ([base] if base else []))
        for a in node.names:
            self.info.imports.append(ImportInfo(base, a.name, a.asname or a.name, node.lineno))

    # names ---------------------------------------------------------------
    def visit_Name(self, node: ast.Name) -> None:
        self.info.used_names.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for t in node.targets:
            is_all = isinstance(t, ast.Name) and t.id == "__all__"
            if is_all and isinstance(node.value, (ast.List, ast.Tuple)):
                self.info.exported |= {e.value for e in node.value.elts
                                       if isinstance(e, ast.Constant) and isinstance(e.value, str)}
        self.generic_visit(node)

    # definitions ---------------------------------------------------------
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qual = ".".join(self.scope + [node.name])
        methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        self.info.classes.append(ClassInfo(
            f"{self.info.name}:{qual}", self.info.name, node.name, self.info.file, node.lineno,
            [ast.unparse(b) for b in node.bases], methods))
        for d in node.decorator_list:
            self.visit(d)
        for b in node.bases:
            self.visit(b)
        self.scope.append(node.name)
        self.class_stack.append(node.name)
        for stmt in node.body:
            self.visit(stmt)
        self.class_stack.pop()
        self.scope.pop()

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qual = ".".join(self.scope + [node.name])
        in_class = bool(self.class_stack) and self.scope and self.scope[-1] == self.class_stack[-1]
        calls = sorted({t for n in _walk_own_body(node) if isinstance(n, ast.Call) and (t := call_target(n))})
        self.info.functions.append(FunctionInfo(
            qualname=f"{self.info.name}:{qual}",
            module=self.info.name,
            name=node.name,
            file=self.info.file,
            lineno=node.lineno,
            end_lineno=node.end_lineno or node.lineno,
            params=_params(node.args),
            returns=_unparse(node.returns),
            is_method=bool(in_class),
            is_async=isinstance(node, ast.AsyncFunctionDef),
            decorators=[ast.unparse(d) for d in node.decorator_list],
            docstring=ast.get_docstring(node),
            complexity=cyclomatic_complexity(node),
            max_nesting=max_nesting(node),
            raw_calls=calls,
            has_try=any(isinstance(n, ast.Try) for n in _walk_own_body(node)),
            body_hash=_body_hash(node),
            parent_class=self.class_stack[-1] if in_class else None,
        ))
        for d in node.decorator_list:
            self.visit(d)
        for a in node.args.args + node.args.kwonlyargs:
            if a.annotation:
                self.visit(a.annotation)
        if node.returns:
            self.visit(node.returns)
        self.scope.append(node.name)
        saved = self.class_stack
        self.class_stack = []  # methods of a class defined inside a function are tracked separately
        for stmt in node.body:
            self.visit(stmt)
        self.class_stack = saved
        self.scope.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function


def analyse_source(source: str, module: str, file: str) -> ModuleInfo:
    info = ModuleInfo(name=module, file=file)
    try:
        tree = ast.parse(source, filename=file)
    except SyntaxError as exc:
        info.parse_error = f"SyntaxError line {exc.lineno}: {exc.msg}"
        log.warning("could not parse file", extra={"file": file, "error": info.parse_error})
        return info
    package = module if file.endswith("__init__.py") else module.rpartition(".")[0]
    _ModuleVisitor(info, package).visit(tree)
    return info


def analyse_repository(root: Path, inventory: Inventory) -> RepoModel:
    model = RepoModel(root=str(root))
    for rec in inventory.python_sources():
        source = (root / rec.path).read_text(encoding="utf-8", errors="replace")
        name = module_name_for(rec.path)
        model.modules[name] = analyse_source(source, name, rec.path)
    log.info("analysed repository", extra=model.summary())
    return model
