import ast
import textwrap

import pytest

from codepilot.analysis import analyse_repository
from codepilot.errors import LLMUnavailableError
from codepilot.ingestion import scan
from codepilot.llmtests import build_prompt, extract_code, generate_llm_tests, unsafe_reasons

OPS = textwrap.dedent(
    """\
    import pathlib


    def clamp(x, lo=0, hi=10):
        if x < lo:
            return lo
        if x > hi:
            return hi
        return x


    def toggle():
        # Alternates between processes: a stand-in for real order- or state-dependent code.
        p = pathlib.Path(".toggle")
        n = int(p.read_text()) if p.exists() else 0
        p.write_text(str(n + 1))
        return n % 2 == 0
    """
)

GOOD_AND_BAD = '''Here are the tests:
```python
import pytest
from calc.ops import clamp, toggle


def test_inside():
    assert clamp(5) == 5


def test_boundaries():
    assert clamp(10) == 10
    assert clamp(11) == 10


@pytest.mark.parametrize(("x", "want"), [(0, 0), (20, 10)])
def test_param(x, want):
    assert clamp(x) == want


def test_wrong_expectation():
    assert clamp(-1) == -1


def test_missing_name():
    assert clamp_all([1]) == [1]


def test_toggle():
    assert toggle() is True


def test_shell():
    import subprocess
    subprocess.run(["ls"])


def test_random():
    assert clamp(random.randint(0, 5)) <= 10
```
'''


class Fake:
    name = "fake"

    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.prompts = reply, error, []

    def complete(self, system, prompt):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.reply


@pytest.fixture
def calc(tmp_path):
    (tmp_path / "calc").mkdir()
    (tmp_path / "calc" / "__init__.py").write_text("")
    (tmp_path / "calc" / "ops.py").write_text(OPS)
    return tmp_path, analyse_repository(tmp_path, scan(tmp_path))


def test_every_gate_classifies_its_failure(calc):
    repo, model = calc
    r = generate_llm_tests(repo, model, "calc.ops", Fake(GOOD_AND_BAD), runs=3)
    assert r.generated == 8
    assert r.kept == 3
    assert dict(r.rejected) == {
        "unsafe": ["test_random", "test_shell"],
        "wrong-assumption": ["test_wrong_expectation"],
        "invalid-test": ["test_missing_name"],
        "flaky": ["test_toggle"],
    }
    kept = {n.name for n in ast.parse(r.source).body if isinstance(n, ast.FunctionDef)}
    assert kept == {"test_inside", "test_boundaries", "test_param"}
    assert not (repo / ".toggle").exists()  # all runs happened in a disposable copy


def test_prompt_has_source_and_boundary_hints(calc):
    repo, model = calc
    prompt = build_prompt(model, "calc.ops", OPS)
    assert "def clamp(x, lo=0, hi=10):" in prompt
    assert "- clamp(x, lo, hi)" in prompt and "- toggle()" in prompt


def test_module_level_unsafe_import_rejects_everything(calc):
    repo, model = calc
    reply = "```python\nimport os\nfrom calc.ops import clamp\n\ndef test_a():\n    assert clamp(1) == 1\n```"
    r = generate_llm_tests(repo, model, "calc.ops", Fake(reply))
    assert r.kept == 0 and r.rejected["unsafe"] == ["test_a"] and "imports os" in r.note


def test_collection_error_marks_all_invalid(calc):
    repo, model = calc
    reply = "```python\nfrom calc.ops import nope\n\ndef test_a():\n    assert nope()\n```"
    r = generate_llm_tests(repo, model, "calc.ops", Fake(reply), runs=2)
    assert r.kept == 0 and r.rejected["invalid-test"] == ["test_a"]


@pytest.mark.parametrize(
    ("fake", "note"),
    [
        (Fake(error=LLMUnavailableError("timeout")), "LLM unavailable"),
        (Fake("I cannot help with that."), "no parsable"),
        (Fake("```python\ndef test_(:\n```"), "no parsable"),
    ],
)
def test_llm_failures_become_notes(calc, fake, note):
    repo, model = calc
    r = generate_llm_tests(repo, model, "calc.ops", fake)
    assert r.kept == 0 and note in r.note


def test_runs_must_allow_flake_detection(calc):
    repo, model = calc
    with pytest.raises(ValueError):
        generate_llm_tests(repo, model, "calc.ops", Fake(GOOD_AND_BAD), runs=1)


def test_extract_code_variants():
    assert extract_code("```py\ndef test_a():\n    pass\n```") == "def test_a():\n    pass\n"
    assert extract_code("def test_a():\n    pass\n") == "def test_a():\n    pass\n"
    assert extract_code("x = 1") is None


@pytest.mark.parametrize(
    ("src", "reason"),
    [
        ("def test_a():\n    eval('1')\n", "calls eval()"),
        ("def test_a():\n    open('f')\n", "calls open()"),
        ("def test_a():\n    x.__globals__\n", "touches __globals__"),
        ("from . import x\n", "imports from"),
        ("import socket\n", "imports socket"),
    ],
)
def test_unsafe_reasons(src, reason):
    reasons = unsafe_reasons(ast.parse(src), "calc.ops")
    assert reason in " ".join(reasons.values())


def test_safe_imports_allowed():
    src = "import math\nimport pytest\nfrom calc.ops import clamp\nfrom hypothesis import given\n"
    assert unsafe_reasons(ast.parse(src), "calc.ops") == {}
