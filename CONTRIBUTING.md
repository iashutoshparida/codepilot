# Contributing

## Workflow

1. Branch from `main`: `git switch -c feat/<short-name>` or `fix/<short-name>`.
2. Keep commits small and explain *why* in the message.
3. Run the checks before pushing:
   ```bash
   ruff check src tests
   mypy
   coverage run -m pytest && coverage report --fail-under=85
   ```
4. Open a pull request. CI must be green, and a reviewer must approve before merge.
5. Rebase on `main` to resolve conflicts; do not force-push to `main`.

## Code rules

* Business logic goes in the domain modules, not in API routes or CLI commands.
* Every new quality rule, mutation operator or endpoint comes with tests, including a negative case.
* Type hints on public functions; `mypy` must pass.
* No LLM output is trusted without a deterministic check.
* Never commit secrets. Configuration comes from environment variables.

## Releases

Versions follow semantic versioning. Update `CHANGELOG.md` and `src/codepilot/__init__.py`, then tag `vX.Y.Z`.
