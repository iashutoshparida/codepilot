"""Domain errors. The API layer maps each one to an HTTP status."""


class CodePilotError(Exception):
    """Base class for expected, user-facing failures."""


class RepositoryNotFoundError(CodePilotError):
    """The given path does not exist or is not a directory."""


class PathNotAllowedError(CodePilotError):
    """The path is outside the configured allowed roots."""


class SymbolNotFoundError(CodePilotError):
    """A requested function, class or module is not in the analysed repository."""


class LLMUnavailableError(CodePilotError):
    """No LLM provider is configured, or the provider failed or timed out."""


class PatchRejectedError(CodePilotError):
    """A patch could not be applied, or was applied without approval."""
