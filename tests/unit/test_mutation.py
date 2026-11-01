import ast

import pytest

from codepilot.mutation import InjectedBug, apply, discover, run_bug_injection, run_mutation
from tests.conftest import write_tree

SRC = '''
def grade(score):
    """Docstring constants are never mutated: 100."""
    if score >= 90 and score <= 100:
        return "A"
    if not score:
        return "zero"
    return score + 1


if __name__ == "__main__":
    print(grade(5) == 3)
'''


def test_discovers_expected_operators_and_skips_docstrings_and_main():
    ops = sorted({m.operator for m in discover(SRC, "g.py")})
    assert ops == ["arithmetic", "comparison", "constant", "logical", "negation", "return"]
    lines = {m.line for m in discover(SRC, "g.py")}
    assert 3 not in lines and 11 not in lines and 12 not in lines


def test_each_mutant_changes_exactly_one_thing():
    mutants = discover(SRC, "g.py")
    originals = ast.dump(ast.parse(SRC))
    seen = set()
    for m in mutants:
        out = apply(SRC, m.id)
        assert ast.dump(ast.parse(out)) != originals
        seen.add(out)
    assert len(seen) == len(mutants)


def test_comparison_mutant_content():
    m = next(m for m in discover(SRC, "g.py") if m.description == "GtE -> Gt")
    assert "score > 90" in apply(SRC, m.id)


@pytest.fixture
def repo(tmp_path):
    return write_tree(tmp_path, {
        "calc.py": "def add(a, b):\n    return a + b\n\n\ndef is_adult(age):\n    return age >= 18\n",
        "tests/test_weak.py": "from calc import add, is_adult\n\ndef test_add():\n    assert add(2, 0) == 2\n",
        "tests/test_strong.py": (
            "from calc import add, is_adult\n\n"
            "def test_add():\n    assert add(2, 3) == 5\n\n"
            "def test_adult_boundary():\n    assert is_adult(18) and not is_adult(17)\n"),
    })


def test_strong_suite_kills_more_mutants_than_weak(repo):
    weak = run_mutation(repo, ["calc.py"], ["tests/test_weak.py"])
    strong = run_mutation(repo, ["calc.py"], ["tests/test_strong.py"])
    assert weak.total == strong.total == 5
    assert strong.score > weak.score
    assert strong.killed == 5 and strong.survived == 0
    assert any(m.description == "GtE -> Gt" for m in weak.survivors())


def test_red_baseline_is_refused(repo):
    with pytest.raises(RuntimeError):
        run_mutation(repo, ["calc.py"], ["tests/test_x.py"],
                     extra_files={"tests/test_x.py": "def test_x():\n    assert False\n"})


def test_bug_injection(repo):
    bugs = [
        InjectedBug("off-by-one", "calc.py", "age >= 18", "age > 18", "boundary moved"),
        InjectedBug("wrong-op", "calc.py", "a + b", "a - b", "subtracts instead of adds"),
    ]
    weak = run_bug_injection(repo, bugs, ["tests/test_weak.py"])
    strong = run_bug_injection(repo, bugs, ["tests/test_strong.py"])
    assert weak["detected"] == 0 and strong["detected"] == 2 and strong["detection_rate"] == 100.0
    with pytest.raises(ValueError):
        run_bug_injection(repo, [InjectedBug("x", "calc.py", "not-present", "y", "")], ["tests/test_strong.py"])
