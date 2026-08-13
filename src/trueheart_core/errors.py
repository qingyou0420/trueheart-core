"""Stable, body-free exceptions for TrueHeart Core."""


class TrueHeartError(Exception):
    """Base class for errors exposed by TrueHeart Core."""


class ValidationError(TrueHeartError):
    """An input field violated a public contract."""

    def __init__(self, field: str, rule: str) -> None:
        super().__init__(f"{field}: {rule}")


class EntityNotFound(TrueHeartError):
    """An entity identifier was not found."""

    def __init__(self, entity_id: str) -> None:
        super().__init__(f"entity not found: {entity_id}")


class ScopeMismatch(TrueHeartError):
    """An entity identifier does not belong to the requested scope."""

    def __init__(self, entity_id: str) -> None:
        super().__init__(f"scope mismatch: {entity_id}")


class IdempotencyConflict(TrueHeartError):
    """An identifier was reused with a different canonical request."""

    def __init__(self, entity_id: str) -> None:
        super().__init__(f"idempotency conflict: {entity_id}")


class EntityDeleted(TrueHeartError):
    """An identifier is blocked by an irreversible tombstone."""

    def __init__(self, entity_id: str) -> None:
        super().__init__(f"entity deleted: {entity_id}")


class InvalidTransition(TrueHeartError):
    """A lifecycle action is invalid for an entity identifier."""

    def __init__(self, entity_id: str) -> None:
        super().__init__(f"invalid transition: {entity_id}")


class TrustEscalation(TrueHeartError):
    """A memory identifier exceeds the trust of its sources."""

    def __init__(self, entity_id: str) -> None:
        super().__init__(f"trust escalation: {entity_id}")


class RepositoryCorruption(TrueHeartError):
    """The repository violated a fixed integrity diagnostic."""

    def __init__(self, diagnostic: str) -> None:
        super().__init__(f"repository corruption: {diagnostic}")
