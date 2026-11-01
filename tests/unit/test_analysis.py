import ast
import textwrap

import pytest

from codepilot.analysis import analyse_source, cyclomatic_complexity, max_nesting, module_name_for

SRC = textwrap.dedent('''
    """module doc"""
    import os
    import json as js
    from collections import Counter
    from .util import helper, other as renamed
    from ..core import base

    class Store(Base):
        """A store."""
        def add(self, item: str, qty: int = 1) -> None:
            self.check(item)
            helper(item)

        def check(self, item):
            if not item:
                raise ValueError("empty")

        class Inner:
            def deep(self):
                return 1

    async def fetch(url, *args, timeout=5, **kw):
        return await os.path.join(url)

    def outer(x):
        def inner(y):
            return y + 1
        return inner(x)
''')


@pytest.fixture
def info():
    return analyse_source(SRC, "pkg.sub.mod", "pkg/sub/mod.py")


def test_functions_methods_and_nested_qualnames(info):
    names = {f.qualname for f in info.functions}
    assert names == {
        "pkg.sub.mod:Store.add", "pkg.sub.mod:Store.check", "pkg.sub.mod:Store.Inner.deep",
        "pkg.sub.mod:fetch", "pkg.sub.mod:outer", "pkg.sub.mod:outer.inner",
    }
    by = {f.qualname: f for f in info.functions}
    assert by["pkg.sub.mod:Store.add"].is_method and by["pkg.sub.mod:Store.add"].parent_class == "Store"
    assert by["pkg.sub.mod:Store.Inner.deep"].parent_class == "Inner"
    assert not by["pkg.sub.mod:outer.inner"].is_method
    assert by["pkg.sub.mod:fetch"].is_async


def test_classes_and_bases(info):
    store = next(c for c in info.classes if c.name == "Store")
    assert store.bases == ["Base"] and store.methods == ["add", "check"]
    assert {c.qualname for c in info.classes} == {"pkg.sub.mod:Store", "pkg.sub.mod:Store.Inner"}


def test_signatures(info):
    add = next(f for f in info.functions if f.name == "add")
    assert [(p.name, p.annotation, p.has_default, p.default) for p in add.params] == [
        ("self", None, False, None), ("item", "str", False, None), ("qty", "int", True, "1")]
    assert add.returns == "None"
    fetch = next(f for f in info.functions if f.name == "fetch")
    assert [p.kind for p in fetch.params] == ["positional", "vararg", "kwonly", "kwarg"]


def test_imports_resolve_relative(info):
    imps = {(i.module, i.name, i.alias) for i in info.imports}
    assert ("os", None, "os") in imps
    assert ("json", None, "js") in imps
    assert ("collections", "Counter", "Counter") in imps
    assert ("pkg.sub.util", "helper", "helper") in imps
    assert ("pkg.sub.util", "other", "renamed") in imps
    assert ("pkg.core", "base", "base") in imps


def test_calls_exclude_nested_function_bodies(info):
    by = {f.name: f for f in info.functions}
    assert by["add"].raw_calls == ["helper", "self.check"]
    assert by["outer"].raw_calls == ["inner"]
    assert by["fetch"].raw_calls == ["os.path.join"]


def test_syntax_error_is_recorded_not_raised():
    m = analyse_source("def broken(:\n  pass\n", "bad", "bad.py")
    assert m.parse_error and "SyntaxError" in m.parse_error and m.functions == []


@pytest.mark.parametrize("code,expected", [
    ("def f():\n    return 1\n", 1),
    ("def f(x):\n    if x:\n        return 1\n    return 2\n", 2),
    ("def f(x, y):\n    if x and y or x:\n        pass\n", 4),
    ("def f(xs):\n    for x in xs:\n        while x:\n            x -= 1\n", 3),
    ("def f(xs):\n    return [x for x in xs if x if x > 1]\n", 4),
    ("def f():\n    try:\n        pass\n    except A:\n        pass\n    except B:\n        pass\n", 3),
    ("def f(x):\n    def g():\n        if x:\n            pass\n    return g\n", 1),
])
def test_cyclomatic_complexity(code, expected):
    fn = ast.parse(code).body[0]
    assert cyclomatic_complexity(fn) == expected


def test_max_nesting():
    code = "def f(x):\n    if x:\n        for i in x:\n            with open(i):\n                pass\n"
    assert max_nesting(ast.parse(code).body[0]) == 3


@pytest.mark.parametrize("path,name", [
    ("pkg/mod.py", "pkg.mod"), ("src/pkg/mod.py", "pkg.mod"), ("pkg/__init__.py", "pkg"), ("app.py", "app"),
])
def test_module_names(path, name):
    assert module_name_for(path) == name


def test_duplicate_bodies_share_hash():
    src = "def a(x):\n    '''doc'''\n    return x * 2\n\ndef b(x):\n    return x * 2\n"
    a = analyse_source(src, "m", "m.py")
    h = {f.name: f.body_hash for f in a.functions}
    assert h["a"] == h["b"]
