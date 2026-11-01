# Testing strategy

## Levels

| Level | Where | What it covers |
|---|---|---|
| Unit | `tests/unit/` | Each module in isolation with small generated repositories (`tests/conftest.py`) |
| Integration | `tests/integration/test_api.py` | Every API route through FastAPI's `TestClient` with the service and LLM provider injected |
| Integration | `tests/integration/test_cli_evaluate.py` | CLI commands and the full evaluate pipeline on a tiny repository |
| End-to-end | CI `evaluation` job | `codepilot evaluate` on `examples/inventory_app`, with thresholds on mutation score and bugs caught |

97 tests, 93% line and branch coverage of `src/codepilot` (`coverage run -m pytest && coverage report`). CI fails below 85%.

## Techniques used

* **Fixtures**: `small_repo` builds a package with cross-module calls, aliased imports, a class, recursion and a deliberate import cycle.
* **Boundary and negative tests**: oversized files, empty repositories, invalid paths, path traversal, syntax errors in source and in tests, timeouts, invalid patch ids, empty reviewer names.
* **Parametrised tests**: complexity scores, classification rules, module naming.
* **Mocking at the boundary**: LLM HTTP calls go through `httpx.MockTransport` (success, timeout and retry, rate-limit retry, HTTP error, malformed payload). The API test swaps the service with `app.dependency_overrides`.
* **Regression tests**: generated tests are shown to catch an injected regression; the refactor workflow is shown to block a change that drops `qty` from a subtotal.
* **Property-based testing**: CodePilot writes Hypothesis tests for typed functions; its own tests check those are produced and dropped when they fail on unmodified code.
* **Mutation testing**: the engine is tested on a weak and a strong suite (the strong one must kill every mutant, the weak one must leave the `>=` to `>` mutant alive).

## Validation scenarios checked

| Scenario | Test |
|---|---|
| Ingestion across nested folders | `test_scan_multiple_directories_and_kinds` |
| Empty repository | `test_empty_repository` |
| Invalid path, path outside allowed roots | `test_invalid_path_raises`, `test_path_outside_allowed_roots_is_refused`, `test_path_errors` |
| Nested functions and classes | `test_functions_methods_and_nested_qualnames` |
| Import extraction including relative imports | `test_imports_resolve_relative` |
| Call graph across modules and aliases | `test_cross_module_call_edges` |
| Circular imports | `test_module_graph_and_cycle` |
| Static analysis on seeded problems | `test_each_seeded_problem_is_found` |
| LLM failure and timeout | `test_timeout_retries_then_fails`, `test_explain_and_llm_failure` |
| Generated test execution | `test_generate_validate_and_report_edge_cases` |
| Invalid generated tests | `test_candidate_test_classification` |
| Regression after refactor | `test_regressing_refactor_is_blocked_and_original_untouched` |
| API validation errors | `test_request_validation` |
| Auth and secret handling | `test_auth_is_required`, `test_server_without_key_refuses`, `test_secrets_are_not_passed_to_child` |
| CI pipeline | `.github/workflows/ci.yml` |

## Measuring generated tests

Test count is not the measure. `codepilot evaluate` reports, for the original suite and for original + generated:

1. pass/fail of every test on unmodified code (generated tests that fail are dropped and classified);
2. line and branch coverage;
3. mutation score over every mutant of the source files;
4. how many of a list of known, realistic bugs are detected.

Current results are in [docs/EVALUATION.md](docs/EVALUATION.md).

## Running locally

```bash
ruff check src tests
mypy
coverage run -m pytest && coverage report
codepilot evaluate examples/inventory_app --tests tests --bugs examples/inventory_app/bugs.json
```
