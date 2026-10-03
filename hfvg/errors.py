"""Typed error classes for workflow error handling and retry policies."""

from temporalio.exceptions import ApplicationError


class RetryableError(ApplicationError):
    """Retryable errors: timeouts, rate limits, transient failures."""

    def __init__(self, message: str, *args, **kwargs):
        super().__init__(message, *args, **kwargs)


class ContentBlockError(ApplicationError):
    """Content moderation block - non-retryable, requires recovery branch."""

    def __init__(self, message: str, provider: str, *args, **kwargs):
        super().__init__(message, *args, non_retryable=True, **kwargs)
        self.provider = provider


class InsufficientCreditsError(ApplicationError):
    """Insufficient credits - non-retryable, pause and notify user."""

    def __init__(self, message: str, required: float, available: float, *args, **kwargs):
        super().__init__(message, *args, non_retryable=True, **kwargs)
        self.required = required
        self.available = available


class FatalError(ApplicationError):
    """Fatal error - non-retryable, pause and escalate."""

    def __init__(self, message: str, *args, **kwargs):
        super().__init__(message, *args, non_retryable=True, **kwargs)
