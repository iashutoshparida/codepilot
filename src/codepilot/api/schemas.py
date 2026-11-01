"""Request and response models. Validation happens here, before any handler runs."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

QUALNAME = r"^[A-Za-z_][\w.]*:[A-Za-z_][\w.]*$"
MODULE = r"^[A-Za-z_][\w.]*$"


def _relative(paths: list[str]) -> list[str]:
    for p in paths:
        if p.startswith(("/", "\\")) or ".." in p.replace("\\", "/").split("/") or ":" in p:
            raise ValueError(f"paths must be relative to the repository and stay inside it: {p}")
    return paths


class RepoRequest(BaseModel):
    repo_path: str = Field(min_length=1, max_length=4096)


class ImpactRequest(RepoRequest):
    qualname: str = Field(pattern=QUALNAME, max_length=300)
    max_depth: int | None = Field(default=None, ge=1, le=50)


class QualityRequest(RepoRequest):
    min_severity: str = Field(default="info", pattern="^(info|warning|error)$")


class ExplainRequest(RepoRequest):
    qualname: str = Field(pattern=QUALNAME, max_length=300)
    question: str | None = Field(default=None, max_length=2000)


class GenerateTestsRequest(RepoRequest):
    module: str = Field(pattern=MODULE, max_length=300)
    validate_tests: bool = True


class RunTestsRequest(RepoRequest):
    test_paths: list[str] = Field(default_factory=lambda: ["tests"], min_length=1, max_length=50)
    coverage: bool = True

    _check = field_validator("test_paths")(lambda cls, v: _relative(v))


class ValidateTestsRequest(RepoRequest):
    test_source: str = Field(min_length=1, max_length=200_000)


class MutationRequest(RepoRequest):
    source_files: list[str] = Field(min_length=1, max_length=50)
    test_paths: list[str] = Field(default_factory=lambda: ["tests"], min_length=1, max_length=50)
    max_mutants: int = Field(default=100, ge=1, le=1000)

    _check_src = field_validator("source_files")(lambda cls, v: _relative(v))
    _check_tests = field_validator("test_paths")(lambda cls, v: _relative(v))


class ProposeRequest(RepoRequest):
    qualname: str = Field(pattern=QUALNAME, max_length=300)
    new_code: str | None = Field(default=None, max_length=100_000, description="Replacement written by a person")
    instruction: str | None = Field(default=None, max_length=2000, description="Ask the LLM to write the change")
    test_paths: list[str] = Field(default_factory=lambda: ["tests"], min_length=1, max_length=50)

    _check = field_validator("test_paths")(lambda cls, v: _relative(v))


class ApproveRequest(RepoRequest):
    approved_by: str = Field(min_length=1, max_length=100)
    test_paths: list[str] = Field(default_factory=lambda: ["tests"], min_length=1, max_length=50)

    _check = field_validator("test_paths")(lambda cls, v: _relative(v))


class PatchActionRequest(RepoRequest):
    pass
