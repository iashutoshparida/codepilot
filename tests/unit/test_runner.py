import os

from codepilot.runner import run_tests
from codepilot.sandbox import scrubbed_env
from tests.conftest import write_tree


def test_runs_suite_and_reports_coverage(small_repo):
    r = run_tests(small_repo)
    assert (r.passed, r.failed, r.errors) == (1, 0, 0) and r.green
    assert r.line_coverage is not None and 0 < r.line_coverage < 100
    assert r.branch_coverage is not None


def test_failing_and_erroring_tests_are_distinguished(small_repo):
    extra = {"tests/test_extra.py": (
        "import pytest\n"
        "from shop.pricing import subtotal\n"
        "def test_wrong():\n    assert subtotal([]) == 1\n"
        "@pytest.fixture\ndef broken():\n    raise RuntimeError('boom')\n"
        "def test_error(broken):\n    pass\n")}
    r = run_tests(small_repo, extra_files=extra, with_coverage=False)
    by = {k.split("::")[-1]: v for k, v in r.by_test().items()}
    assert by["test_wrong"] == "failed" and by["test_error"] == "error" and by["test_checkout"] == "passed"
    assert not r.green


def test_syntax_error_in_test_file_is_a_collection_error(small_repo):
    r = run_tests(small_repo, extra_files={"tests/test_bad.py": "def test_x(:\n"}, with_coverage=False)
    assert r.errors >= 1 and not r.green


def test_timeout_is_reported(tmp_path):
    root = write_tree(tmp_path, {"tests/test_slow.py": "import time\ndef test_s():\n    time.sleep(5)\n"})
    r = run_tests(root, timeout_s=1, with_coverage=False)
    assert r.timed_out and not r.green


def test_original_repo_is_not_modified(small_repo):
    before = sorted(p.name for p in small_repo.rglob("*"))
    run_tests(small_repo, extra_files={"tests/test_new.py": "def test_ok():\n    assert True\n"})
    assert sorted(p.name for p in small_repo.rglob("*")) == before


def test_secrets_are_not_passed_to_child(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("SOME_TOKEN", "t")
    env = scrubbed_env()
    assert "OPENAI_API_KEY" not in env and "SOME_TOKEN" not in env and "PATH" in os.environ
