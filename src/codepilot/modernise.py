"""Outdated-pattern detection: APIs and idioms that are deprecated, removed in recent Python
versions, or have a simpler modern replacement. Each finding names the replacement.

Severity: "warning" when the code breaks or warns on a supported Python version,
"info" when the old form still works but has a clearer modern equivalent.
"""
from __future__ import annotations

import ast

from .quality import Finding

# module -> (severity, why, replacement)
REMOVED_MODULES = {
    "imp": ("warning", "removed in Python 3.12", "importlib"),
    "distutils": ("warning", "removed in Python 3.12", "setuptools / packaging"),
    "asynchat": ("warning", "removed in Python 3.12", "asyncio"),
    "asyncore": ("warning", "removed in Python 3.12", "asyncio"),
    "smtpd": ("warning", "removed in Python 3.12", "aiosmtpd"),
    "cgi": ("warning", "removed in Python 3.13", "email.message / urllib.parse"),
    "cgitb": ("warning", "removed in Python 3.13", "traceback / logging"),
    "pipes": ("warning", "removed in Python 3.13", "shlex.quote"),
    "telnetlib": ("warning", "removed in Python 3.13", "telnetlib3 or asyncssh"),
    "imghdr": ("warning", "removed in Python 3.13", "filetype or Pillow"),
    "nntplib": ("warning", "removed in Python 3.13", "a maintained NNTP library"),
    "optparse": ("info", "superseded since Python 3.2", "argparse"),
    "six": ("info", "Python 2 compatibility shim", "plain Python 3 code"),
    "pkg_resources": ("warning", "deprecated by setuptools", "importlib.metadata / importlib.resources"),
}
COLLECTIONS_ABCS = {"Mapping", "MutableMapping", "Sequence", "MutableSequence", "Set",
                    "MutableSet", "Iterable", "Iterator", "Callable", "Hashable", "Sized",
                    "Container", "Generator", "Awaitable", "Coroutine"}
TYPING_GENERICS = {"List": "list", "Dict": "dict", "Set": "set", "FrozenSet": "frozenset",
                   "Tuple": "tuple", "Type": "type", "DefaultDict": "collections.defaultdict",
                   "Deque": "collections.deque"}
# dotted call -> (severity, message)
DEPRECATED_CALLS = {
    "datetime.utcnow": ("warning", "deprecated in 3.12 and returns a naive datetime; use "
                                   "datetime.now(timezone.utc)"),
    "datetime.datetime.utcnow": ("warning", "deprecated in 3.12; use datetime.now(timezone.utc)"),
    "datetime.utcfromtimestamp": ("warning", "deprecated in 3.12; use "
                                             "datetime.fromtimestamp(ts, timezone.utc)"),
    "asyncio.get_event_loop": ("warning", "deprecated outside a running loop; use asyncio.run() "
                                          "or asyncio.get_running_loop()"),
    "logging.warn": ("warning", "deprecated alias; use logging.warning"),
    "os.popen": ("info", "use subprocess.run with an argument list"),
    "locale.getdefaultlocale": ("warning", "deprecated in 3.11; use locale.getlocale"),
}
UNITTEST_ALIASES = {"assertEquals": "assertEqual", "assertNotEquals": "assertNotEqual",
                    "assertRegexpMatches": "assertRegex", "assertAlmostEquals": "assertAlmostEqual",
                    "failUnless": "assertTrue", "failIf": "assertFalse",
                    "assertItemsEqual": "assertCountEqual"}


class _Visitor(ast.NodeVisitor):
    def __init__(self, module: str, file: str) -> None:
        self.module, self.file = module, file
        self.out: list[Finding] = []
        self.scope: list[str] = []

    def _add(self, sev: str, node: ast.AST, msg: str) -> None:
        sym = f"{self.module}:{'.'.join(self.scope)}" if self.scope else None
        self.out.append(Finding("outdated-api", sev, self.file, getattr(node, "lineno", 0), sym, msg))

    def _module(self, name: str, node: ast.AST) -> None:
        root = name.split(".")[0]
        if root in REMOVED_MODULES:
            sev, why, repl = REMOVED_MODULES[root]
            self._add(sev, node, f"'{root}' is {why}; use {repl}")

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._module(alias.name, node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module is None or node.level:
            return
        self._module(node.module, node)
        names = {a.name for a in node.names}
        if node.module == "collections" and names & COLLECTIONS_ABCS:
            self._add("warning", node, f"importing {', '.join(sorted(names & COLLECTIONS_ABCS))} "
                                       "from 'collections' fails since 3.10; use collections.abc")
        if node.module == "typing" and names & set(TYPING_GENERICS):
            repl = ", ".join(f"{n} -> {TYPING_GENERICS[n]}" for n in sorted(names & set(TYPING_GENERICS)))
            self._add("info", node, f"typing aliases are deprecated since 3.9; use built-ins ({repl})")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if any(isinstance(b, ast.Name) and b.id == "object" for b in node.bases):
            self._add("info", node, f"'class {node.name}(object)' is a Python 2 idiom; "
                                    f"write 'class {node.name}:'")
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for dec in node.decorator_list:
            if ast.unparse(dec) == "asyncio.coroutine":
                self._add("warning", dec, "@asyncio.coroutine was removed in 3.11; use 'async def'")
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        name = ast.unparse(node.func)
        if name in DEPRECATED_CALLS:
            sev, msg = DEPRECATED_CALLS[name]
            self._add(sev, node, f"{name}(): {msg}")
        elif isinstance(node.func, ast.Attribute) and node.func.attr in UNITTEST_ALIASES \
                and ast.unparse(node.func.value) == "self":
            new = UNITTEST_ALIASES[node.func.attr]
            self._add("warning", node, f"{node.func.attr} is a deprecated alias (removed in 3.12 "
                                       f"for most); use {new}")
        elif name == "super" and len(node.args) == 2:
            self._add("info", node, "super(Class, self) can be written super()")
        self.generic_visit(node)


def outdated_patterns(tree: ast.AST, module: str, file: str) -> list[Finding]:
    visitor = _Visitor(module, file)
    visitor.visit(tree)
    return visitor.out
