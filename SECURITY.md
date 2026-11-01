# Security

## Threat model

CodePilot runs code from the repositories it analyses (to probe behaviour and run tests). That code is untrusted. API callers may be untrusted too.

## Controls

| Risk | Control |
|---|---|
| Unauthenticated use of the API | `X-API-Key` on every `/api/v1` route, constant-time comparison; server refuses protected routes if no key is configured |
| Reading arbitrary paths | `repo_path` resolved (symlinks and `..` collapsed) and checked against `CODEPILOT_ALLOWED_ROOTS`; `test_paths` and `source_files` must be relative and contain no `..`; LLM citations outside the repository are rejected |
| Analysed code changing the original repository | Tests, probes and mutants run in a disposable copy; the original only changes when an approved patch passes its tests |
| Secrets leaking to analysed code | Child processes get an environment without any variable whose name contains KEY, TOKEN, SECRET, PASSWORD or CREDENTIAL |
| Runaway code | Wall-clock timeouts on every subprocess; a 2 s alarm per probed call |
| Secrets in the repository | Keys only from environment variables; `.env` is git-ignored; `.env.example` holds placeholders |
| Unreviewed AI changes | Patches are proposals; applying needs a named approver and a green before/after test run; backups allow rollback |
| Hallucinated AI explanations | Citations and mentioned function names are checked against the real source; `grounded` is false when anything fails the check |
| Vulnerable dependencies | `pip-audit` runs in CI |
| Container | Non-root user (uid 10001), slim base image, `no-new-privileges` in compose |

Static checks also flag `eval`, `exec`, `pickle.load(s)`, `yaml.load`, `os.system` and `subprocess` with `shell=True` in analysed code.

## Known gaps

* The subprocess sandbox does not block network or filesystem access outside the copy. Run untrusted repositories only inside the Docker image, ideally with `--network none`.
* There is a single shared API key, no per-user identity and no rate limiting.
* Patch state is stored as JSON files and is not safe for concurrent writers.

## Reporting

Please report security issues privately to the maintainer rather than opening a public issue.
