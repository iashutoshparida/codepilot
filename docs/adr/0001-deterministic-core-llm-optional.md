# ADR 0001: Deterministic core, LLM optional

**Status:** accepted

**Context.** LLMs explain code well but can invent functions, line numbers and defects. A tool engineers rely on before changing code must give the same answer every time.

**Decision.** Ingestion, analysis, graphs, quality rules, test generation and evaluation are deterministic and work with no LLM configured. The LLM only explains code and drafts patches. Its prompts contain only facts CodePilot extracted, and its answers are checked: citations must point at real lines, mentioned functions must exist. Patches go through the same approval and test gate as human changes.

**Consequences.** CodePilot is useful offline and in CI. AI features return 503 when no provider is configured instead of degrading silently.
