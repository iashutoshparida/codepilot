"""Settings loaded from environment variables. Secrets never have defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import PathNotAllowedError, RepositoryNotFoundError


def _roots_from_env() -> list[Path]:
    raw = os.environ.get("CODEPILOT_ALLOWED_ROOTS", "")
    return [Path(p).expanduser().resolve() for p in raw.split(os.pathsep) if p.strip()]


def _float_env(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    api_key: str | None = field(default_factory=lambda: os.environ.get("CODEPILOT_API_KEY") or None)
    allowed_roots: list[Path] = field(default_factory=_roots_from_env)
    llm_provider: str = field(default_factory=lambda: os.environ.get("CODEPILOT_LLM_PROVIDER", "none"))
    llm_model: str = field(default_factory=lambda: os.environ.get("CODEPILOT_LLM_MODEL", "gpt-4o-mini"))
    llm_timeout_s: float = field(default_factory=lambda: _float_env("CODEPILOT_LLM_TIMEOUT_S", 30))
    test_timeout_s: float = field(default_factory=lambda: _float_env("CODEPILOT_TEST_TIMEOUT_S", 120))

    def resolve_repo(self, path: str | Path) -> Path:
        """Resolve a user-supplied path and refuse anything outside the allowed roots.

        With no roots configured (local CLI use) any existing directory is accepted.
        """
        p = Path(path).expanduser().resolve()
        if not p.exists() or not p.is_dir():
            raise RepositoryNotFoundError(f"Not a directory: {path}")
        if self.allowed_roots and not any(p == r or r in p.parents for r in self.allowed_roots):
            raise PathNotAllowedError(f"Path is outside the allowed roots: {path}")
        return p
