import pytest

from codepilot.analysis import analyse_repository
from codepilot.graphs import build_graphs
from codepilot.ingestion import scan
from codepilot.quality import Thresholds, analyse_quality, summarise
from tests.conftest import write_tree

BAD = {
    "app/__init__.py": "",
    "app/bad.py": """
        import os
        import json
        import pickle
        from typing import List

        def risky(data, items=[]):
            try:
                x = eval(data)
            except:
                x = None
            if x == None:
                return pickle.loads(data)
            os.system("ls " + data)
            return items

        def nested(a):
            if a:
                for i in a:
                    while i:
                        if i > 2:
                            with open("f") as fh:
                                return fh.read()
            return None

        def read_cfg(path):
            return json.loads(open(path).read())

        def quiet():
            try:
                return 1
            except Exception:
                pass

        def copy_one(v):
            total = 0
            for i in range(v):
                total += i * 2
            return total

        def copy_two(v):
            total = 0
            for i in range(v):
                total += i * 2
            return total

        def many(a, b, c, d, e, f, g):
            return a
    """,
}


@pytest.fixture
def findings(tmp_path):
    root = write_tree(tmp_path, BAD)
    model = analyse_repository(root, scan(root))
    return analyse_quality(root, model, build_graphs(model), Thresholds(max_nesting=3))


def _rules(findings):
    return {(f.rule, f.symbol.split(":")[-1] if f.symbol else None) for f in findings}


def test_each_seeded_problem_is_found(findings):
    r = _rules(findings)
    assert ("unsafe-call", "risky") in r                     # eval, pickle.loads, os.system
    assert sum(f.rule == "unsafe-call" for f in findings) == 3
    assert ("bare-except", "risky") in r
    assert ("mutable-default", "risky") in r
    assert ("none-comparison", "risky") in r
    assert ("deep-nesting", "app.bad:nested") in {(f.rule, f.symbol) for f in findings}
    assert ("swallowed-exception", "quiet") in r
    assert ("duplicate-code", "app.bad:copy_one") in {(f.rule, f.symbol) for f in findings}
    assert ("too-many-params", "app.bad:many") in {(f.rule, f.symbol) for f in findings}
    assert ("unhandled-io", "app.bad:read_cfg") in {(f.rule, f.symbol) for f in findings}


def test_unused_import_found_but_used_ones_not(findings):
    unused = {f.message.split("'")[1] for f in findings if f.rule == "unused-import"}
    assert unused == {"List"}


def test_errors_sorted_first_and_summary(findings):
    assert findings[0].severity == "error"
    s = summarise(findings)
    assert s["total"] == len(findings) and s["by_severity"]["error"] == 3


def test_clean_code_has_no_findings(tmp_path):
    root = write_tree(tmp_path, {"ok.py": "def add(a, b):\n    return a + b\n"})
    model = analyse_repository(root, scan(root))
    assert analyse_quality(root, model, build_graphs(model)) == []


def test_import_cycle_and_parse_error_reported(small_repo):
    (small_repo / "shop" / "broken.py").write_text("def f(:\n")
    model = analyse_repository(small_repo, scan(small_repo))
    rules = {f.rule for f in analyse_quality(small_repo, model, build_graphs(model))}
    assert {"import-cycle", "parse-error"} <= rules


def test_subprocess_without_shell_is_not_flagged(tmp_path):
    root = write_tree(tmp_path, {"r.py": "import subprocess\n\ndef go():\n    subprocess.run(['ls'])\n"})
    model = analyse_repository(root, scan(root))
    assert not [f for f in analyse_quality(root, model) if f.rule == "unsafe-call"]
