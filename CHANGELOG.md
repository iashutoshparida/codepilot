# Changelog

## 0.1.0 - 2026-11-01

First release.

### Added
- Repository ingestion with language and file-kind detection.
- AST analysis: functions, methods, classes, signatures, imports, calls, complexity, nesting.
- Call graph and module graph with change-impact, cycle and recursion queries.
- Deterministic quality rules (13 rule types).
- Sandboxed pytest runner with line and branch coverage.
- Behaviour-based test generation with Hypothesis property tests and validation of generated tests.
- Boundary-value analysis: inputs at c-1, c and c+1 for constants each function compares against. On the sample repository this raised the mutation score of the generated suite from 70.0% to 87.8% and injected bugs caught from 7 to 10 of 12.
- AST mutation testing (6 operator families) and known-bug injection.
- LLM provider abstraction (OpenAI, Anthropic) with grounded explanations and citation checks.
- Approval-gated refactoring with before/after test comparison, backup and rollback.
- FastAPI backend with API-key auth, path allow-list and validation; CLI; Docker; GitHub Actions CI.
