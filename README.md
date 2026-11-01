# CodePilot

CodePilot helps engineers work safely in unfamiliar Python codebases. It maps a repository, finds risky code, writes regression tests from the code's real behaviour, measures whether those tests would catch bugs, and runs refactors behind a human approval step that blocks any change that breaks a test.

Deterministic analysis does the checking. An LLM is optional and only explains code or drafts patches; its output is verified before anyone relies on it.

## Why

Before changing legacy code you need to know three things: what calls this, what tests protect it, and whether those tests would notice if you broke it. Most legacy repositories cannot answer the third question. CodePilot answers all three and then helps you close the gap.

## Results on the sample legacy repository

`examples/inventory_app` is a small inventory and ordering codebase with a three-test suite, seeded code smells, one real edge-case bug and 12 realistic injected bugs (`bugs.json`). `codepilot evaluate` produced these numbers (full report: [docs/EVALUATION.md](docs/EVALUATION.md)):

| Metric | Original tests | Original + CodePilot tests |
|---|---|---|
| Tests | 3 | 169 |
| Line coverage | 33.9% | 95.8% |
| Branch coverage | 3.9% | 85.0% |
| Mutation score (90 mutants) | 7.8% | 87.8% |
| Injected bugs caught | 0 / 12 | 10 / 12 |

It also flagged two real crashes (`average_order_value([])` and `days_of_cover(level, 0)` raise `ZeroDivisionError`) as findings for review rather than writing tests that lock them in, and reported all 9 seeded maintainability problems.

The two bugs it misses are honest limits of behaviour-based generation: a rounding-order change and a SKU-normalisation change both need domain-specific inputs the generator does not invent.

## Features

| Area | What it does | Module |
|---|---|---|
| Ingestion | Walks a local repository, skips virtualenvs, build output and VCS folders, classifies source, test, config and docs, counts languages | `ingestion.py` |
| AST analysis | Functions, methods, nested functions, classes, signatures with types and defaults, imports (relative imports resolved), calls, cyclomatic complexity, nesting depth | `analysis.py` |
| Graphs | Call graph and module dependency graph (NetworkX); change-impact by distance, circular imports, recursion, fan-out | `graphs.py` |
| Static checks | Long or complex functions, deep nesting, duplicate bodies, unused imports, bare or swallowed exceptions, mutable defaults, `eval`/`exec`/`pickle`/`shell=True`, unhandled I/O, import cycles | `quality.py` |
| Test generation | Typical, boundary (c-1, c, c+1 from the code's own comparisons), empty and invalid inputs; runs the function in a sandbox; writes characterisation tests and Hypothesis property tests; drops any test that fails on the unmodified code | `testgen.py` |
| Test validation | Classifies candidate tests from any source (LLM or human) as keep, wrong-assumption or invalid-test | `testgen.py` |
| Effectiveness | Line and branch coverage, AST mutation testing (6 operator families), known-bug injection | `runner.py`, `mutation.py` |
| Refactoring | Propose, review diff, approve, test before and after on a copy, apply only if nothing regresses, keep a backup, roll back | `refactor.py` |
| Outdated patterns | Modules removed in Python 3.12/3.13 (`imp`, `distutils`, `cgi`, `pipes`...), deprecated calls (`datetime.utcnow`, `asyncio.get_event_loop`, `logging.warn`), `collections` ABC imports, `typing.List`-style aliases, unittest aliases, `class X(object)`, `super(C, self)`, each with its modern replacement | `modernise.py` |
| LLM test generation | LLM-written pytest tests pass four gates before they are kept: they must parse; they must be safe (no shell, network, files or randomness); they must pass on the unmodified code (failures classified as wrong-assumption / invalid-test / behaviour-mismatch); and they must be stable across repeated runs in fresh processes (flaky tests are dropped) | `llmtests.py` |
| PR testing | `codepilot pr-tests --base origin/main`: finds the Python files a PR changed, generates and validates tests for them, and reports the mutation score of the changed code before and after; runs on every pull request via `.github/workflows/pr-tests.yml` | `prtests.py` |
| AI assistance | Provider abstraction (OpenAI, Anthropic, none), grounded prompts built from the analysis, citation and symbol checks on every answer | `llm.py`, `assistant.py` |
| API | FastAPI with API-key auth, path allow-list, typed validation, consistent errors, OpenAPI docs | `api/` |

## Architecture

```
            CLI (cli.py)          FastAPI (api/app.py)
                  \                    /
                   CodePilotService (service.py)  <- caches analysis per repository
          /         |          |            \              \
   ingestion -> analysis -> graphs -> quality      assistant -> llm (provider protocol)
                     \                                /
                      testgen  ->  runner / sandbox  <-  mutation, refactor, evaluate
```

Details and trade-offs: [ARCHITECTURE.md](ARCHITECTURE.md) and [docs/adr](docs/adr).

## Setup

Requires Python 3.11+.

```bash
git clone https://github.com/iashutoshparida/codepilot.git
cd codepilot
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # set CODEPILOT_API_KEY; LLM keys are optional
pytest                      # 144 tests
```

Docker:

```bash
docker compose up --build   # API on http://localhost:8000, docs at /docs
```

## Usage

```bash
codepilot analyse examples/inventory_app
codepilot impact  examples/inventory_app inventory.pricing:add_gst
codepilot quality examples/inventory_app
codepilot gen-tests examples/inventory_app inventory.pricing --write
codepilot mutate  examples/inventory_app --source inventory/pricing.py --tests tests
codepilot evaluate examples/inventory_app --tests tests --bugs examples/inventory_app/bugs.json \
    --markdown docs/EVALUATION.md
codepilot serve
```

API example:

```bash
curl -s -X POST localhost:8000/api/v1/impact -H "X-API-Key: $CODEPILOT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"repo_path": "/workspace/examples/inventory_app", "qualname": "inventory.pricing:add_gst"}'
```

All endpoints: [API_DOCUMENTATION.md](API_DOCUMENTATION.md).

## Testing

144 tests (unit and integration), 92% line and branch coverage of CodePilot itself, `ruff` and `mypy` clean. CI runs them on Python 3.11 and 3.12, re-runs the sample evaluation and fails if the mutation score drops below 80% or fewer than 9 injected bugs are caught, audits dependencies, and builds and smoke-tests the Docker image. See [TESTING_STRATEGY.md](TESTING_STRATEGY.md).

## Limitations

* Python only. Other languages are inventoried, not parsed.
* Call resolution is static: calls through instances (`obj.method()`), dynamic dispatch and monkey-patching are reported as unresolved, not guessed.
* Test generation covers module-level functions with simple inputs. Methods, async functions, generators and functions needing objects or I/O are skipped with a stated reason.
* Characterisation tests pin current behaviour, bugs included. They protect refactors; they do not prove correctness.
* The sandbox is process isolation with timeouts and scrubbed secrets, not a security boundary. Use the Docker image for untrusted code.
* Long jobs (mutation, evaluation) run synchronously in the request.

## Future work

Class and method test generation with fixture synthesis; incremental mutation testing on changed lines only; a background job queue; a tree-sitter front end for Java and C#; LLM-proposed test inputs, validated the same way as generated tests.

## Licence

MIT
