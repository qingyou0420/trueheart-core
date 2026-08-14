"""Public contracts for governed long-term memory."""

from importlib.metadata import version

from .domain import (
    AuditRecord,
    EntityType,
    GovernanceAction,
    GovernanceCommand,
    GovernanceResult,
    MemoryDraft,
    MemoryRecord,
    MemoryStatus,
    RawEventDraft,
    RawEventReceipt,
    RecallItem,
    RecallQuery,
    RetentionPolicy,
    Scope,
    SourceRef,
    TrustLevel,
)
from .errors import (
    EntityDeleted,
    EntityNotFound,
    IdempotencyConflict,
    InvalidTransition,
    RepositoryBusy,
    RepositoryCorruption,
    ScopeMismatch,
    TrueHeartError,
    TrustEscalation,
    ValidationError,
)
from .service import TrueHeart
from .sqlite import SQLiteRepository

__version__ = version("trueheart-core")

__all__ = [
    "__version__",
    "AuditRecord",
    "EntityDeleted",
    "EntityNotFound",
    "EntityType",
    "GovernanceAction",
    "GovernanceCommand",
    "GovernanceResult",
    "IdempotencyConflict",
    "InvalidTransition",
    "MemoryDraft",
    "MemoryRecord",
    "MemoryStatus",
    "RawEventDraft",
    "RawEventReceipt",
    "RecallItem",
    "RecallQuery",
    "RepositoryBusy",
    "RepositoryCorruption",
    "RetentionPolicy",
    "SQLiteRepository",
    "Scope",
    "ScopeMismatch",
    "SourceRef",
    "TrueHeart",
    "TrueHeartError",
    "TrustEscalation",
    "TrustLevel",
    "ValidationError",
]
