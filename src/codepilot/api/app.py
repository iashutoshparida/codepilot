"""Module 9: FastAPI backend. Routes stay thin; the work happens in the service layer."""
from __future__ import annotations

import logging
import secrets
from dataclasses import asdict
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..assistant import build_proposal
from ..config import Settings
from ..errors import (
    CodePilotError,
    LLMUnavailableError,
    PatchRejectedError,
    PathNotAllowedError,
    RepositoryNotFoundError,
    SymbolNotFoundError,
)
from ..mutation import run_mutation
from ..quality import summarise
from ..runner import run_tests
from ..service import CodePilotService
from ..testgen import generate_for_module, validate_candidate_tests
from . import schemas as s

log = logging.getLogger("codepilot.api")
STATUS = {RepositoryNotFoundError: 404, SymbolNotFoundError: 404, PathNotAllowedError: 403,
          LLMUnavailableError: 503, PatchRejectedError: 409}


@lru_cache
def get_service() -> CodePilotService:
    return CodePilotService(Settings())


def require_api_key(service: Annotated[CodePilotService, Depends(get_service)],
                    x_api_key: Annotated[str | None, Header()] = None) -> None:
    expected = service.settings.api_key
    if not expected:
        raise HTTPException(503, "server has no CODEPILOT_API_KEY configured")
    if not x_api_key or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(401, "missing or invalid X-API-Key", headers={"WWW-Authenticate": "ApiKey"})


Service = Annotated[CodePilotService, Depends(get_service)]
app = FastAPI(title="CodePilot", version=__version__,
              description="Repository analysis, change impact and validated test generation for Python code.")
api = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])  # auth on every route


def _handle(request: Request, exc: Exception) -> JSONResponse:
    status = next((code for cls, code in STATUS.items() if isinstance(exc, cls)), 400)
    log.info("request failed", extra={"path": request.url.path, "status": status, "error": type(exc).__name__})
    return JSONResponse(status_code=status, content={"error": type(exc).__name__, "detail": str(exc)})


app.add_exception_handler(CodePilotError, _handle)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@api.post("/analysis")
def analysis(req: s.RepoRequest, svc: Service) -> dict:
    a = svc.analyse(req.repo_path)
    q = summarise(a.findings)
    return {"inventory": {k: v for k, v in a.inventory.to_dict().items() if k != "files"},
            "model": a.model.summary(), "quality": {"total": q["total"], "by_severity": q["by_severity"]},
            "module_cycles": a.graphs.module_cycles(), "recursive_functions": a.graphs.recursive_functions()}


@api.post("/inventory")
def inventory(req: s.RepoRequest, svc: Service) -> dict:
    return svc.analyse(req.repo_path).inventory.to_dict()


@api.post("/graph")
def graph(req: s.RepoRequest, svc: Service) -> dict:
    return svc.analyse(req.repo_path).graphs.to_dict()


@api.post("/impact")
def impact(req: s.ImpactRequest, svc: Service) -> dict:
    return svc.analyse(req.repo_path).graphs.impact(req.qualname, req.max_depth)


@api.post("/quality")
def quality(req: s.QualityRequest, svc: Service) -> dict:
    order = {"error": 0, "warning": 1, "info": 2}
    keep = [f for f in svc.analyse(req.repo_path).findings if order[f.severity] <= order[req.min_severity]]
    return summarise(keep)


@api.post("/explain")
def explain(req: s.ExplainRequest, svc: Service) -> dict:
    ans = svc.assistant(req.repo_path).explain(req.qualname, req.question)
    return asdict(ans) | {"grounded": ans.grounded}


@api.post("/tests/generate")
def generate_tests(req: s.GenerateTestsRequest, svc: Service) -> dict:
    a = svc.analyse(req.repo_path)
    if req.module not in a.model.modules:
        raise SymbolNotFoundError(f"Unknown module: {req.module}")
    return generate_for_module(a.root, a.model, req.module, req.validate_tests, svc.settings.test_timeout_s).to_dict()


@api.post("/tests/run")
def run(req: s.RunTestsRequest, svc: Service) -> dict:
    return run_tests(svc.resolve(req.repo_path), req.test_paths, timeout_s=svc.settings.test_timeout_s,
                     with_coverage=req.coverage).to_dict()


@api.post("/tests/validate")
def validate(req: s.ValidateTestsRequest, svc: Service) -> dict:
    return validate_candidate_tests(svc.resolve(req.repo_path), req.test_source, timeout_s=svc.settings.test_timeout_s)


@api.post("/mutation")
def mutation(req: s.MutationRequest, svc: Service) -> dict:
    root = svc.resolve(req.repo_path)
    for f in req.source_files:
        if not (root / f).is_file():
            raise HTTPException(422, f"source file not found: {f}")
    try:
        return run_mutation(root, req.source_files, req.test_paths, max_mutants=req.max_mutants).to_dict()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@api.post("/refactor/propose")
def propose(req: s.ProposeRequest, svc: Service) -> dict:
    if bool(req.new_code) == bool(req.instruction):
        raise HTTPException(422, "send exactly one of new_code or instruction")
    assistant = svc.assistant(req.repo_path)
    if req.new_code:
        proposal = build_proposal(assistant.root, assistant.function(req.qualname), req.new_code)
    else:
        proposal = assistant.propose_patch(req.qualname, req.instruction or "")
    rec = svc.workflow(req.repo_path, req.test_paths).propose(proposal)
    return asdict(rec)


@api.get("/refactor")
def list_patches(repo_path: str, svc: Service) -> list[dict]:
    return [asdict(r) for r in svc.workflow(repo_path, ["tests"]).store.list()]


@api.post("/refactor/{patch_id}/approve")
def approve(patch_id: str, req: s.ApproveRequest, svc: Service) -> dict:
    graphs = svc.analyse(req.repo_path).graphs
    return asdict(svc.workflow(req.repo_path, req.test_paths).approve(patch_id, req.approved_by, graphs))


@api.post("/refactor/{patch_id}/reject")
def reject(patch_id: str, req: s.PatchActionRequest, svc: Service) -> dict:
    return asdict(svc.workflow(req.repo_path, ["tests"]).reject(patch_id))


@api.post("/refactor/{patch_id}/rollback")
def rollback(patch_id: str, req: s.PatchActionRequest, svc: Service) -> dict:
    return asdict(svc.workflow(req.repo_path, ["tests"]).rollback(patch_id))


app.include_router(api)
