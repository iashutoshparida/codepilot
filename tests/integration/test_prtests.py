import shutil
import subprocess
from pathlib import Path

import pytest

from codepilot.errors import CodePilotError
from codepilot.prtests import analyse_pr_tests, changed_python_files

SAMPLE = Path(__file__).resolve().parents[2] / "examples" / "inventory_app"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def pr_repo(tmp_path):
    repo = tmp_path / "app"
    shutil.copytree(SAMPLE, repo, ignore=shutil.ignore_patterns("__pycache__"))
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "switch", "-qc", "feature")
    pricing = repo / "inventory" / "pricing.py"
    pricing.write_text(pricing.read_text() + "\n\ndef loyalty_points(amount):\n"
                       "    if amount < 0:\n        raise ValueError('amount')\n"
                       "    return int(amount // 100)\n")
    (repo / "tests" / "test_extra.py").write_text("def test_nothing():\n    assert True\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add loyalty points")
    return repo


def test_only_changed_source_files_are_selected(pr_repo):
    assert changed_python_files(pr_repo, "main") == ["inventory/pricing.py"]


def test_generated_tests_raise_mutation_score_on_changed_code(pr_repo):
    report = analyse_pr_tests(pr_repo, "main", ["tests"], max_mutants=40)
    [module] = report.modules
    assert module.module == "inventory.pricing" and module.kept > 0
    assert report.mutation_before is not None and report.mutation_after is not None
    assert report.mutation_after > report.mutation_before
    md = report.to_markdown()
    assert "Mutation score on changed files" in md and "`inventory.pricing`" in md


def test_no_python_changes(pr_repo):
    git(pr_repo, "switch", "-qc", "docs", "main")
    (pr_repo / "NOTES.md").write_text("notes\n")
    git(pr_repo, "add", "-A")
    git(pr_repo, "commit", "-qm", "docs")
    report = analyse_pr_tests(pr_repo, "main", ["tests"])
    assert report.modules == [] and "No changed Python" in report.to_markdown()


@pytest.mark.parametrize("ref", ["--output=x", "", "a b"])
def test_bad_refs_rejected(pr_repo, ref):
    with pytest.raises(CodePilotError):
        changed_python_files(pr_repo, ref)


def test_unknown_ref_is_an_error(pr_repo):
    with pytest.raises(CodePilotError):
        changed_python_files(pr_repo, "no-such-branch")
