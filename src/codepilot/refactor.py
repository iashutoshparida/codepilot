"""Module 8: human-approved refactoring with regression checks and rollback.

Flow: propose -> (human reviews diff) -> approve -> tests run on a copy before and after
-> only if nothing regresses is the original file changed (with a backup) -> rollback
restores the backup. A patch is never applied without an explicit approval.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .assistant import PatchProposal
from .errors import PatchRejectedError
from .graphs import Graphs
from .runner import RunReport, run_tests_in
from .sandbox import workspace

log = logging.getLogger("codepilot.refactor")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class PatchRecord:
    id: str
    qualname: str
    file: str
    start_line: int
    end_line: int
    original: str
    replacement: str
    diff: str
    warnings: list[str]
    file_sha_at_proposal: str
    status: str = "pending"            # pending | applied | rejected | regressed | rolled_back
    created_at: float = field(default_factory=time.time)
    approved_by: str | None = None
    validation: dict | None = None
    patched_sha: str | None = None


class PatchStore:
    """Patches and backups live under <state_dir>, outside the analysed source tree."""

    def __init__(self, state_dir: Path):
        self.dir = state_dir / "patches"
        self.backups = state_dir / "backups"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.backups.mkdir(parents=True, exist_ok=True)

    def save(self, rec: PatchRecord) -> None:
        (self.dir / f"{rec.id}.json").write_text(json.dumps(asdict(rec), indent=2))

    def get(self, patch_id: str) -> PatchRecord:
        if not re.fullmatch(r"[0-9a-f]{12}", patch_id):
            raise PatchRejectedError("invalid patch id")
        p = self.dir / f"{patch_id}.json"
        if not p.exists():
            raise PatchRejectedError(f"unknown patch {patch_id}")
        return PatchRecord(**json.loads(p.read_text()))

    def list(self) -> list[PatchRecord]:
        return sorted((PatchRecord(**json.loads(p.read_text())) for p in self.dir.glob("*.json")),
                      key=lambda r: r.created_at)


def _apply_text(text: str, rec: PatchRecord) -> str:
    lines = text.splitlines(keepends=True)
    current = "".join(lines[rec.start_line - 1: rec.end_line])
    if current != rec.original:
        raise PatchRejectedError("the function has changed since the patch was proposed")
    return "".join(lines[: rec.start_line - 1]) + rec.replacement + "".join(lines[rec.end_line:])


def _tests_referencing(repo: Path, names: set[str], test_dirs: list[str]) -> list[str]:
    hits = set()
    for d in test_dirs:
        base = repo / d
        files = [base] if base.is_file() else base.rglob("*.py")
        for f in files:
            text = f.read_text(encoding="utf-8", errors="replace")
            if any(re.search(rf"\b{re.escape(n)}\b", text) for n in names):
                hits.add(str(f.relative_to(repo)))
    return sorted(hits)


class RefactorWorkflow:
    def __init__(self, repo: Path, state_dir: Path, test_paths: list[str], timeout_s: float = 120):
        self.repo, self.store, self.test_paths, self.timeout_s = repo, PatchStore(state_dir), test_paths, timeout_s

    def propose(self, proposal: PatchProposal) -> PatchRecord:
        text = (self.repo / proposal.file).read_text(encoding="utf-8")
        rec = PatchRecord(id=uuid.uuid4().hex[:12], qualname=proposal.qualname, file=proposal.file,
                          start_line=proposal.start_line, end_line=proposal.end_line, original=proposal.original,
                          replacement=proposal.replacement, diff=proposal.diff, warnings=proposal.warnings,
                          file_sha_at_proposal=_sha(text))
        self.store.save(rec)
        log.info("patch proposed", extra={"patch": rec.id, "symbol": rec.qualname})
        return rec

    def reject(self, patch_id: str) -> PatchRecord:
        rec = self.store.get(patch_id)
        if rec.status != "pending":
            raise PatchRejectedError(f"patch is {rec.status}, not pending")
        rec.status = "rejected"
        self.store.save(rec)
        return rec

    def _run(self, patched_text: str | None, rel: str) -> RunReport:
        with workspace(self.repo) as ws:
            if patched_text is not None:
                (ws / rel).write_text(patched_text)
            return run_tests_in(ws, self.test_paths, self.timeout_s, with_coverage=True)

    def approve(self, patch_id: str, approved_by: str, graphs: Graphs | None = None) -> PatchRecord:
        if not approved_by.strip():
            raise PatchRejectedError("approval needs the reviewer's name")
        rec = self.store.get(patch_id)
        if rec.status != "pending":
            raise PatchRejectedError(f"patch is {rec.status}, not pending")
        path = self.repo / rec.file
        current = path.read_text(encoding="utf-8")
        if _sha(current) != rec.file_sha_at_proposal:
            raise PatchRejectedError(f"{rec.file} changed after the patch was proposed; propose it again")
        patched = _apply_text(current, rec)

        before, after = self._run(None, rec.file), self._run(patched, rec.file)
        b, a = before.by_test(), after.by_test()
        regressions = sorted(t for t, o in b.items() if o == "passed" and a.get(t) != "passed")
        fixed = sorted(t for t, o in a.items() if o == "passed" and b.get(t) not in (None, "passed"))
        names = {rec.qualname.split(":")[1].split(".")[-1]}
        impact = graphs.impact(rec.qualname) if graphs is not None and rec.qualname in graphs.calls else None
        if impact:
            names |= {x["qualname"].split(":")[1].split(".")[-1] for x in impact["affected_functions"]}
        rec.validation = {
            "before": {k: before.to_dict()[k] for k in ("passed", "failed", "errors", "line_coverage")},
            "after": {k: after.to_dict()[k] for k in ("passed", "failed", "errors", "line_coverage")},
            "regressions": regressions, "newly_passing": fixed,
            "change_impact": impact,
            "tests_referencing_changed_code": _tests_referencing(self.repo, names, self.test_paths),
            "warning": None if before.total else "no tests ran: the change is unverified",
        }
        rec.approved_by = approved_by
        if regressions or after.timed_out or not after.total:
            rec.status = "regressed"
            log.warning("patch blocked by regressions", extra={"patch": rec.id, "regressions": len(regressions)})
        else:
            (self.store.backups / f"{rec.id}.orig").write_text(current)
            path.write_text(patched)
            rec.patched_sha = _sha(patched)
            rec.status = "applied"
            log.info("patch applied", extra={"patch": rec.id, "by": approved_by})
        self.store.save(rec)
        return rec

    def rollback(self, patch_id: str) -> PatchRecord:
        rec = self.store.get(patch_id)
        if rec.status != "applied":
            raise PatchRejectedError(f"only applied patches can be rolled back (status: {rec.status})")
        path = self.repo / rec.file
        if _sha(path.read_text(encoding="utf-8")) != rec.patched_sha:
            raise PatchRejectedError(f"{rec.file} was edited after the patch; roll back by hand or with git")
        path.write_text((self.store.backups / f"{rec.id}.orig").read_text())
        rec.status = "rolled_back"
        self.store.save(rec)
        return rec
