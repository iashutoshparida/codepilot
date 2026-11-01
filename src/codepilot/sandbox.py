"""Run untrusted repository code in a separate process.

Analysed code is arbitrary code, so it never runs inside the CodePilot process:
* a throwaway copy of the repository is used, so the original is never modified;
* secrets are stripped from the child's environment;
* every run has a wall-clock timeout.

This is process isolation, not a security boundary: it does not block network or
filesystem access. Run CodePilot inside the provided Docker image for untrusted code.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .ingestion import EXCLUDED_DIRS

SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def scrubbed_env(extra_pythonpath: list[Path] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not any(m in k.upper() for m in SECRET_MARKERS)}
    paths = [str(p) for p in (extra_pythonpath or [])]
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"
    return env


@contextmanager
def workspace(repo: Path) -> Iterator[Path]:
    """A disposable copy of `repo` without virtualenvs, caches or VCS folders."""
    tmp = Path(tempfile.mkdtemp(prefix="codepilot-"))
    dest = tmp / "repo"
    try:
        shutil.copytree(repo, dest, ignore=shutil.ignore_patterns(*EXCLUDED_DIRS, "*.pyc"), symlinks=True)
        yield dest
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def import_roots(repo: Path) -> list[Path]:
    """Directories to put on sys.path so the repo's packages import as they would in production."""
    roots = [repo]
    if (repo / "src").is_dir():
        roots.insert(0, repo / "src")
    return roots


@dataclass
class ProcResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


def run_python(args: list[str], cwd: Path, timeout_s: float, pythonpath: list[Path]) -> ProcResult:
    try:
        p = subprocess.run([sys.executable, *args], cwd=cwd, env=scrubbed_env(pythonpath),
                           capture_output=True, text=True, timeout=timeout_s)
        return ProcResult(p.returncode, p.stdout, p.stderr, False)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return ProcResult(-1, out, f"timed out after {timeout_s}s", True)
