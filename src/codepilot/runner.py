"""Run a pytest suite against a repository copy and collect outcomes and coverage."""
from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .sandbox import import_roots, run_python, workspace

log = logging.getLogger("codepilot.runner")


@dataclass
class TestOutcome:
    nodeid: str
    outcome: str            # passed | failed | error | skipped
    message: str = ""


@dataclass
class RunReport:
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    timed_out: bool = False
    line_coverage: float | None = None
    branch_coverage: float | None = None
    covered_lines: int | None = None
    total_lines: int | None = None
    outcomes: list[TestOutcome] = field(default_factory=list)
    stderr_tail: str = ""

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped

    @property
    def green(self) -> bool:
        return not self.timed_out and self.failed == 0 and self.errors == 0 and self.passed > 0

    def by_test(self) -> dict[str, str]:
        return {o.nodeid: o.outcome for o in self.outcomes}

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total"] = self.total
        d["green"] = self.green
        return d


_COUNTER = {"passed": "passed", "failed": "failed", "error": "errors", "skipped": "skipped"}


def parse_junit(xml_path: Path) -> list[TestOutcome]:
    if not xml_path.exists():
        return []
    out = []
    for case in ET.parse(xml_path).getroot().iter("testcase"):
        cls, name = case.get("classname", ""), case.get("name", "")
        nodeid = f"{cls}::{name}" if cls else name
        outcome, msg = "passed", ""
        for tag in ("failure", "error", "skipped"):
            el = case.find(tag)
            if el is not None:
                outcome = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
                msg = (el.get("message") or el.text or "")[:500]
                break
        out.append(TestOutcome(nodeid, outcome, msg))
    return out


def _coverage(cov_json: Path) -> dict:
    if not cov_json.exists():
        return {}
    totals = json.loads(cov_json.read_text())["totals"]
    branches = totals.get("num_branches") or 0
    return {
        "line_coverage": round(totals["covered_lines"] / max(totals["num_statements"], 1) * 100, 2),
        "branch_coverage": round(totals.get("covered_branches", 0) / branches * 100, 2) if branches else None,
        "covered_lines": totals["covered_lines"],
        "total_lines": totals["num_statements"],
    }


def run_tests_in(ws: Path, test_paths: list[str], timeout_s: float = 120, with_coverage: bool = True,
                 fail_fast: bool = False, omit: list[str] | None = None) -> RunReport:
    """Run pytest inside an existing workspace directory."""
    junit = ws / ".codepilot-junit.xml"
    pytest_args = ["-q", "-p", "no:cacheprovider", f"--junitxml={junit}", "-o", "junit_family=xunit2"]
    if fail_fast:
        pytest_args.append("-x")
    omit_globs = ",".join(["*/tests/*", "*/test_*", "*conftest.py", *(omit or [])])
    if with_coverage:
        args = ["-m", "coverage", "run", "--branch", f"--omit={omit_globs}", "--data-file=.cp-coverage",
                "-m", "pytest", *pytest_args, *test_paths]
    else:
        args = ["-m", "pytest", *pytest_args, *test_paths]
    proc = run_python(args, ws, timeout_s, import_roots(ws))
    report = RunReport(timed_out=proc.timed_out, stderr_tail=(proc.stderr or proc.stdout)[-1500:])
    report.outcomes = parse_junit(junit)
    for o in report.outcomes:
        attr = _COUNTER[o.outcome]
        setattr(report, attr, getattr(report, attr) + 1)
    if not report.outcomes and proc.returncode not in (0, 5) and not proc.timed_out:
        report.errors = 1  # collection failed, e.g. a syntax error in a test file
        report.outcomes = [TestOutcome("<collection>", "error", report.stderr_tail[-500:])]
    if with_coverage and not proc.timed_out:
        cj = ws / ".cp-coverage.json"
        run_python(["-m", "coverage", "json", "--data-file=.cp-coverage", "-o", str(cj), "-q"],
                   ws, 60, import_roots(ws))
        for k, v in _coverage(cj).items():
            setattr(report, k, v)
    log.info("test run", extra={"passed": report.passed, "failed": report.failed, "errors": report.errors})
    return report


def run_tests(repo: Path, test_paths: list[str] | None = None, extra_files: dict[str, str] | None = None,
              timeout_s: float = 120, with_coverage: bool = True) -> RunReport:
    """Copy `repo`, add `extra_files` (relative path -> text), and run the given tests."""
    with workspace(repo) as ws:
        for rel, text in (extra_files or {}).items():
            p = ws / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        return run_tests_in(ws, test_paths or ["tests"], timeout_s, with_coverage)
