# API

Base URL: `http://localhost:8000`. Interactive docs: `/docs` (Swagger) and `/openapi.json`.

## Authentication

Every `/api/v1/*` route needs the header `X-API-Key: <CODEPILOT_API_KEY>`. Keys are compared in constant time. If the server has no key configured, protected routes return `503` rather than running open.

`repo_path` must be an existing directory inside one of `CODEPILOT_ALLOWED_ROOTS` (path traversal such as `../` is resolved before the check). File lists such as `test_paths` must be relative and stay inside the repository.

## Errors

All handled errors return `{"error": "<Type>", "detail": "<message>"}`.

| Status | When |
|---|---|
| 401 | Missing or wrong API key |
| 403 | `repo_path` outside the allowed roots |
| 404 | Repository, module or function not found |
| 409 | Patch not pending, file changed since proposal, or test suite red before mutation testing |
| 422 | Request body failed validation |
| 503 | No API key configured on the server, or the LLM provider is unavailable or timed out |

## Endpoints

| Method | Path | Body (besides `repo_path`) | Returns |
|---|---|---|---|
| GET | `/health` | none, no auth | status and version |
| POST | `/api/v1/analysis` | | inventory counts, model summary, finding counts, import cycles, recursive functions |
| POST | `/api/v1/inventory` | | every file with language, kind, size and lines |
| POST | `/api/v1/graph` | | call edges, module edges, cycles, unresolved calls |
| POST | `/api/v1/impact` | `qualname`, `max_depth?` | functions affected by a change, with distance; affected and importing modules |
| POST | `/api/v1/quality` | `min_severity` (`info`/`warning`/`error`) | findings with rule, severity, file, line, symbol |
| POST | `/api/v1/explain` | `qualname`, `question?` | answer, references, valid and invalid citations, unknown mentions, `grounded` |
| POST | `/api/v1/tests/generate` | `module`, `validate_tests` | test source, kept/dropped tests with reasons, edge-case findings |
| POST | `/api/v1/tests/run` | `test_paths`, `coverage` | pass/fail/error counts, per-test outcomes, line and branch coverage |
| POST | `/api/v1/tests/validate` | `test_source` | per-test verdict: keep, wrong-assumption, invalid-test, behaviour-mismatch |
| POST | `/api/v1/mutation` | `source_files`, `test_paths`, `max_mutants` | score, killed, survivors, results by operator |
| POST | `/api/v1/refactor/propose` | `qualname`, exactly one of `new_code` / `instruction`, `test_paths` | pending patch with diff and warnings |
| GET | `/api/v1/refactor?repo_path=` | | all patches for the repository |
| POST | `/api/v1/refactor/{id}/approve` | `approved_by`, `test_paths` | `applied` or `regressed`, before/after results, change impact |
| POST | `/api/v1/refactor/{id}/reject` | | patch marked rejected |
| POST | `/api/v1/refactor/{id}/rollback` | | original file restored |

`qualname` format: `package.module:Class.method` or `package.module:function`.

## Example: safe refactor

```bash
H='-H X-API-Key:dev-key -H Content-Type:application/json'
R='"repo_path":"/workspace/examples/inventory_app"'

curl -s $H localhost:8000/api/v1/refactor/propose -d "{$R,\"qualname\":\"inventory.reports:total_units\",
  \"new_code\":\"def total_units(orders):\n    return sum(o['qty'] for o in orders)\n\"}"
# -> {"id": "3f1c...", "status": "pending", "diff": "--- a/inventory/reports.py ..."}

curl -s $H localhost:8000/api/v1/refactor/3f1c.../approve -d "{$R,\"approved_by\":\"ashutosh\"}"
# -> {"status": "applied", "validation": {"regressions": [], "before": {...}, "after": {...}}}
```
