# ADR 0005: Approval-gated refactoring

**Status:** accepted

**Context.** AI-drafted or hand-written changes to legacy code can break callers that nobody remembered.

**Decision.** A patch is first stored as a proposal with a diff and warnings (for example a changed signature). Approval requires a reviewer name. On approval the suite runs on a copy before and after the change; the original file is written only if no previously passing test fails. A backup is kept, and rollback refuses to overwrite later edits. The response includes the change-impact report from the call graph.

**Consequences.** Nothing lands without a person and a green run. The check is only as strong as the tests, which is why test generation and mutation scoring sit next to it.
