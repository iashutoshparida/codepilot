"""Module 1: walk a local repository and build a file inventory."""
from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("codepilot.ingestion")

EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env", "node_modules", "build", "dist",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".hypothesis", ".tox", ".idea", ".vscode",
    "site-packages", ".eggs", "target", "out", ".codepilot",
}

LANGUAGES = {
    ".py": "python", ".pyi": "python", ".java": "java", ".kt": "kotlin", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".cs": "csharp", ".js": "javascript",
    ".ts": "typescript", ".go": "go", ".rs": "rust", ".rb": "ruby", ".sql": "sql", ".sh": "shell",
}
CONFIG_NAMES = {
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "tox.ini", "pytest.ini", "Dockerfile",
    "Makefile", "package.json", "pom.xml", "build.gradle", ".env.example",
}
CONFIG_SUFFIXES = {".toml", ".ini", ".cfg", ".yaml", ".yml", ".json"}
MAX_FILE_BYTES = 1_000_000


@dataclass
class FileRecord:
    path: str            # relative to the repository root, POSIX style
    language: str
    kind: str            # source | test | config | doc | other
    size_bytes: int
    lines: int


@dataclass
class Inventory:
    root: str
    files: list[FileRecord] = field(default_factory=list)
    skipped_dirs: list[str] = field(default_factory=list)
    oversized_files: list[str] = field(default_factory=list)

    @property
    def languages(self) -> dict[str, int]:
        return dict(Counter(f.language for f in self.files if f.language != "unknown"))

    @property
    def kinds(self) -> dict[str, int]:
        return dict(Counter(f.kind for f in self.files))

    def python_sources(self) -> list[FileRecord]:
        return [f for f in self.files if f.language == "python" and f.kind == "source"]

    def to_dict(self) -> dict:
        return {
            "root": self.root, "file_count": len(self.files), "languages": self.languages,
            "kinds": self.kinds, "files": [asdict(f) for f in self.files],
            "skipped_dirs": self.skipped_dirs, "oversized_files": self.oversized_files,
        }


def classify(rel: Path) -> str:
    name = rel.name
    parts = {p.lower() for p in rel.parts[:-1]}
    is_test_name = name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"
    if is_test_name or parts & {"tests", "test"}:
        return "test"
    if name in CONFIG_NAMES or rel.suffix in CONFIG_SUFFIXES:
        return "config"
    if rel.suffix in {".md", ".rst", ".txt"}:
        return "doc"
    if rel.suffix in LANGUAGES:
        return "source"
    return "other"


def _count_lines(path: Path) -> int:
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def scan(root: Path) -> Inventory:
    """Walk `root`, skipping vendored and generated folders. Symlinks are not followed."""
    inv = Inventory(root=str(root))
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        kept = []
        for d in dirnames:
            if d in EXCLUDED_DIRS or d.endswith(".egg-info"):
                inv.skipped_dirs.append(str(Path(dirpath, d).relative_to(root).as_posix()))
            else:
                kept.append(d)
        dirnames[:] = sorted(kept)
        for fname in sorted(filenames):
            full = Path(dirpath, fname)
            if full.is_symlink():
                continue
            rel = full.relative_to(root)
            size = full.stat().st_size
            if size > MAX_FILE_BYTES:
                inv.oversized_files.append(rel.as_posix())
                continue
            inv.files.append(FileRecord(
                path=rel.as_posix(),
                language=LANGUAGES.get(full.suffix, "unknown"),
                kind=classify(rel),
                size_bytes=size,
                lines=_count_lines(full),
            ))
    log.info("scanned repository", extra={"root": str(root), "files": len(inv.files)})
    return inv
