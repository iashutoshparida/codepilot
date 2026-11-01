from pathlib import Path

import pytest

from codepilot.config import Settings
from codepilot.errors import PathNotAllowedError, RepositoryNotFoundError
from codepilot.ingestion import classify, scan


def _write(root: Path, rel: str, text: str = "x = 1\n") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_scan_multiple_directories_and_kinds(tmp_path):
    _write(tmp_path, "pkg/core.py", "def f():\n    return 1\n")
    _write(tmp_path, "pkg/sub/util.py")
    _write(tmp_path, "tests/test_core.py")
    _write(tmp_path, "pyproject.toml", "[project]\n")
    _write(tmp_path, "README.md", "# hi\n")
    _write(tmp_path, "src/Main.java", "class Main {}\n")
    inv = scan(tmp_path)
    paths = {f.path: f for f in inv.files}
    assert paths["pkg/core.py"].kind == "source" and paths["pkg/core.py"].lines == 2
    assert paths["pkg/sub/util.py"].kind == "source"
    assert paths["tests/test_core.py"].kind == "test"
    assert paths["pyproject.toml"].kind == "config"
    assert paths["README.md"].kind == "doc"
    assert inv.languages == {"python": 3, "java": 1}
    assert len(inv.python_sources()) == 2


def test_excluded_folders_are_skipped(tmp_path):
    _write(tmp_path, "app.py")
    _write(tmp_path, ".venv/lib/x.py")
    _write(tmp_path, "node_modules/a/index.js")
    _write(tmp_path, "pkg.egg-info/PKG-INFO")
    inv = scan(tmp_path)
    assert [f.path for f in inv.files] == ["app.py"]
    assert {".venv", "node_modules", "pkg.egg-info"} <= set(inv.skipped_dirs)


def test_empty_repository(tmp_path):
    inv = scan(tmp_path)
    assert inv.files == [] and inv.languages == {} and inv.to_dict()["file_count"] == 0


def test_oversized_file_is_listed_not_read(tmp_path):
    (tmp_path / "big.py").write_bytes(b"#" * 1_000_001)
    inv = scan(tmp_path)
    assert inv.files == [] and inv.oversized_files == ["big.py"]


@pytest.mark.parametrize("rel,kind", [
    ("conftest.py", "test"), ("a/b_test.py", "test"), ("setup.cfg", "config"),
    ("x.yaml", "config"), ("notes.txt", "doc"), ("image.png", "other"),
])
def test_classify(rel, kind):
    assert classify(Path(rel)) == kind


def test_invalid_path_raises(tmp_path):
    with pytest.raises(RepositoryNotFoundError):
        Settings(allowed_roots=[]).resolve_repo(tmp_path / "missing")


def test_path_outside_allowed_roots_is_refused(tmp_path):
    inside, outside = tmp_path / "inside", tmp_path / "outside"
    inside.mkdir()
    outside.mkdir()
    s = Settings(allowed_roots=[inside.resolve()])
    assert s.resolve_repo(inside) == inside.resolve()
    with pytest.raises(PathNotAllowedError):
        s.resolve_repo(outside)
    with pytest.raises(PathNotAllowedError):
        s.resolve_repo(inside / ".." / "outside")
