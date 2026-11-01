# ADR 0002: Generate characterisation tests from observed behaviour

**Status:** accepted

**Context.** Asking a model "what should this function return?" produces tests that often fail because the expectation is wrong, not the code. Before a refactor, what matters is whether behaviour changes.

**Decision.** The generator runs each function on chosen inputs and asserts what it actually returned or raised. Every generated test must pass on the unmodified code or it is dropped and classified. Crash-type exceptions (ZeroDivisionError, IndexError, KeyError, ...) are reported as findings instead of being pinned as expected behaviour.

**Consequences.** Generated suites are green by construction and catch regressions well (see docs/EVALUATION.md). They cannot detect bugs already present in the current behaviour; those appear only as edge-case findings for a human.
