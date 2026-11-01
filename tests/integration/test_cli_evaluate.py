import json

from codepilot.cli import main
from codepilot.evaluate import evaluate, to_markdown
from tests.conftest import write_tree

REPO = {
    "calc/__init__.py": "",
    "calc/core.py": "def grade(score: int) -> str:\n    if score >= 50:\n        return 'pass'\n    return 'fail'\n",
    "tests/test_core.py": "from calc.core import grade\n\ndef test_pass():\n    assert grade(80) == 'pass'\n",
    "bugs.json": json.dumps([{"id": "B1", "file": "calc/core.py", "find": "score >= 50", "replace": "score > 50",
                              "description": "boundary"}]),
}


def test_evaluate_improves_a_weak_suite(tmp_path):
    root = write_tree(tmp_path, REPO)
    r = evaluate(root, ["tests"], root / "bugs.json")
    assert r["baseline"]["bug_injection"]["detected"] == 0
    assert r["with_generated"]["bug_injection"]["detected"] == 1
    assert r["with_generated"]["mutation"]["score"] > r["baseline"]["mutation"]["score"]
    md = to_markdown(r)
    assert "| Injected bugs detected | 0/1 | 1/1 |" in md


def test_cli_analyse_and_errors(tmp_path, capsys):
    root = write_tree(tmp_path, REPO)
    assert main(["analyse", str(root)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["model"]["functions"] == 1
    assert main(["impact", str(root), "calc.core:nope"]) == 2
    assert "SymbolNotFoundError" in capsys.readouterr().err


def test_cli_gen_tests_write(tmp_path, capsys):
    root = write_tree(tmp_path, REPO)
    assert main(["gen-tests", str(root), "calc.core", "--write"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["written"] and (root / out["test_file"]).exists() and out["kept_tests"] > 0
