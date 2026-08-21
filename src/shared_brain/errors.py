"""Domain errors shared by the API and storage layers."""


class BrainError(Exception):
    """Base class for expected Shared Brain failures."""


class NotFoundError(BrainError):
    """Requested entity does not exist."""


class ConflictError(BrainError):
    """Optimistic-lock or idempotency conflict."""


class ValidationError(BrainError):
    """Domain validation failed."""

