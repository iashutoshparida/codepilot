# Architecture

## Goals

1. Give an engineer facts about an unfamiliar Python repository: structure, dependencies, risk.
2. Turn current behaviour into a regression safety net before code is changed.
3. Measure test effectiveness with evidence (mutants killed, injected bugs caught), not test counts.
4. Let AI help without letting it decide: every AI output is checked, every change needs approval.

## Layers

```
interfaces     cli.py                api/app.py, api/schemas.py
                  \                    /
application        service.py   (wiring, caching, settings)
                  /    |     |      \
domain     ingestion  analysis  graphs  quality  testgen  mutation  refactor  assistant
                                                     \      |       /
infrastructure                          runner.py, sandbox.py, llm.py, logging_setup.py, config.py
```

* Routes and CLI commands only parse input and format output. Logic lives in domain modules, so the API and CLI behave the same and the logic is tested without HTTP.
* Domain modules depend on plain dataclasses (`RepoModel`, `Graphs`, `Finding`, `RunReport`) instead of on each other's internals.
* External effects (subprocesses, HTTP to LLM providers, the filesystem) sit in the infrastructure layer behind small interfaces, so tests replace them with fakes (`FakeProvider`, `httpx.MockTransport`).

## Data flow

1. **Ingestion** walks the repository (no symlink following, files over 1 MB listed but not read) and returns an `Inventory`.
2. **Analysis** parses each Python file with `ast` into `ModuleInfo` / `FunctionInfo` / `ClassInfo`. Syntax errors are recorded per file and do not stop the run.
3. **Graphs** resolve calls conservatively: same scope and enclosing scopes, `self`/`cls` methods, classes to `__init__`, and names bound by `import x as y` / `from x import y`. Anything else is kept in `external_calls`.
4. **Quality** runs rules on the IR, the raw AST and the call graph. Every finding has a rule id, severity, file and line.
5. **Test generation** builds inputs, probes behaviour in a subprocess, renders tests and validates them (see below).
6. **Runner** executes pytest under `coverage` in a disposable copy and parses JUnit XML and coverage JSON.
7. **Mutation** applies one AST change at a time to the copy and re-runs the suite with `-x`.
8. **Refactor** stores proposals outside the source tree and applies them only after approval and a green before/after comparison.

## Test generation design

```
FunctionInfo + source --> input pools (type hints, defaults, names)
                      --> boundary values (constants the function compares against: c-1, c, c+1)
                      --> one-factor-at-a-time cases (+ both values of bool flags at boundaries)
                      --> probe in subprocess: return value / exception, run twice (determinism)
                      --> render: literal results -> equality asserts (approx for floats)
                                  validation errors -> pytest.raises
                                  crash-type errors -> edge-case findings, no test
                                  typed functions   -> Hypothesis property test
                      --> run on unmodified code; drop and classify anything that fails
```

Expected values are always observed, never guessed. That is what makes the output safe to trust as a regression baseline, and also why it cannot find bugs that the current code already has (those surface as edge-case findings instead).

## Key decisions

| Decision | Why | Record |
|---|---|---|
| Deterministic analysis first, LLM optional | Findings must be reproducible and never invented | [ADR 0001](docs/adr/0001-deterministic-core-llm-optional.md) |
| Characterisation tests from observed behaviour | Generated tests must pass on current code to serve as a refactoring safety net | [ADR 0002](docs/adr/0002-characterisation-tests.md) |
| Own AST mutation engine | Small, dependency-free, easy to explain; operators chosen for common bug types | [ADR 0003](docs/adr/0003-own-mutation-engine.md) |
| Subprocess sandbox on a copy | Analysed code is untrusted and must never modify the original | [ADR 0004](docs/adr/0004-process-sandbox.md) |
| Patches need approval and a green re-run | AI or human changes must not land if they break a test | [ADR 0005](docs/adr/0005-approval-gated-refactoring.md) |

## Extending

* **New language**: add a parser that fills the same `ModuleInfo` dataclasses; graphs and quality rules work on the IR.
* **New quality rule**: add a check in `quality.py` returning `Finding` objects plus a test with a seeded example.
* **New LLM provider**: subclass `_HTTPProvider` (three small methods) or implement the `LLMProvider` protocol.
* **New mutation operator**: add a `visit_*` method to `_Mutator` that calls `_site()`.
