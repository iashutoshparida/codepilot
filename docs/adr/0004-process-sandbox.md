# ADR 0004: Run analysed code in a subprocess on a copy

**Status:** accepted

**Context.** Probing functions and running tests executes repository code, which may be buggy or hostile and must never modify the original.

**Decision.** Copy the repository (without virtualenvs, caches and VCS data) to a temporary directory, run Python in a child process with secrets removed from the environment and a timeout, and delete the copy afterwards.

**Consequences.** Original files are safe and the CodePilot process cannot be crashed by analysed code. This is not a security boundary (no network or filesystem isolation); SECURITY.md tells users to run untrusted code in the container.
