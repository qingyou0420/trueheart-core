"""Immutable public domain contracts and their input validation."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import IntEnum, StrEnum
from types import MappingProxyType
from typing import TypeAlias, cast

from .errors import ValidationError

JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)

_MAX_COMPONENT_LENGTH = 128
_MAX_CONTENT_BYTES = 256 * 1024
_MAX_METADATA_BYTES = 16 * 1024
_MAX_METADATA_DEPTH = 64
_MAX_METADATA_INTEGER_BITS = 4096


class TrustLevel(IntEnum):
    UNTRUSTED = 0
    OBSERVED = 1
    CONFIRMED = 2


class EntityType(StrEnum):
    RAW_EVENT = "raw_event"
    MEMORY = "memory"


class GovernanceAction(StrEnum):
    SEAL = "seal"
    RESTORE = "restore"
    FORGET = "forget"
    DELETE = "delete"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SEALED = "sealed"


def _validate_text(
    value: str, field_name: str, *, max_length: int = _MAX_COMPONENT_LENGTH
) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(field_name, "must be a non-blank string")
    if len(value) > max_length:
        raise ValidationError(field_name, f"must be at most {max_length} characters")


def _normalize_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValidationError(field_name, "must be timezone-aware")
    try:
        offset = value.utcoffset()
        normalized = value.astimezone(UTC)
    except (OSError, OverflowError, RuntimeError, TypeError, ValueError):
        raise ValidationError(field_name, "must be a representable datetime") from None
    if offset is None:
        raise ValidationError(field_name, "must be timezone-aware")
    return normalized


def _freeze_json(value: object, field_name: str, *, depth: int = 0) -> object:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        if value.bit_length() > _MAX_METADATA_INTEGER_BITS:
            raise ValidationError(field_name, "must contain bounded integers")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError(field_name, "must contain finite JSON values")
        return value
    if isinstance(value, list):
        container_depth = depth + 1
        if container_depth > _MAX_METADATA_DEPTH:
            raise ValidationError(field_name, "must be at most 64 levels deep")
        return tuple(
            _freeze_json(item, field_name, depth=container_depth) for item in value
        )
    if isinstance(value, Mapping):
        container_depth = depth + 1
        if container_depth > _MAX_METADATA_DEPTH:
            raise ValidationError(field_name, "must be at most 64 levels deep")
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValidationError(field_name, "must have string keys")
            frozen[key] = _freeze_json(item, field_name, depth=container_depth)
        return MappingProxyType(frozen)
    raise ValidationError(field_name, "must contain only JSON values")


def _json_ready(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _freeze_metadata(value: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise ValidationError("metadata", "must be a JSON object")
    try:
        frozen = _freeze_json(value, "metadata")
        if not isinstance(frozen, Mapping):
            raise ValidationError("metadata", "must be a JSON object")
        if _utf8_length(_canonical_json(frozen), "metadata") > _MAX_METADATA_BYTES:
            raise ValidationError("metadata", "must serialize to at most 16 KiB")
    except ValidationError:
        raise
    except (OverflowError, RecursionError, ValueError):
        raise ValidationError("metadata", "must contain bounded JSON values") from None
    return cast(Mapping[str, JsonValue], frozen)


def _validate_content(content: str) -> None:
    if not isinstance(content, str) or not content:
        raise ValidationError("content", "must be non-empty UTF-8 text")
    if _utf8_length(content, "content") > _MAX_CONTENT_BYTES:
        raise ValidationError("content", "must be at most 256 KiB UTF-8")


def _utf8_length(value: str, field_name: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValidationError(field_name, "must be valid UTF-8") from None


def _validate_trust(trust: TrustLevel) -> None:
    if not isinstance(trust, TrustLevel):
        raise ValidationError("trust", "must be a TrustLevel")


def _validate_positive_retention(value: timedelta, field_name: str) -> None:
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise ValidationError(field_name, "must be positive")


def _validate_source_event_ids(source_event_ids: tuple[str, ...]) -> None:
    if not isinstance(source_event_ids, tuple) or not source_event_ids:
        raise ValidationError("source_event_ids", "must be a non-empty tuple")
    for source_event_id in source_event_ids:
        _validate_text(source_event_id, "source_event_ids")
    if len(set(source_event_ids)) != len(source_event_ids):
        raise ValidationError("source_event_ids", "must not contain duplicates")


@dataclass(frozen=True, slots=True)
class Scope:
    tenant_id: str
    owner_id: str
    subject_id: str

    def __post_init__(self) -> None:
        _validate_text(self.tenant_id, "tenant_id")
        _validate_text(self.owner_id, "owner_id")
        _validate_text(self.subject_id, "subject_id")


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    raw_ttl: timedelta
    clear_for: timedelta
    recall_for: timedelta

    def __post_init__(self) -> None:
        for field_name, value in (
            ("raw_ttl", self.raw_ttl),
            ("clear_for", self.clear_for),
            ("recall_for", self.recall_for),
        ):
            _validate_positive_retention(value, field_name)
        if self.recall_for < self.clear_for:
            raise ValidationError("recall_for", "must be at least clear_for")


@dataclass(frozen=True, slots=True)
class SourceRef:
    source_id: str
    source_type: str
    occurred_at: datetime
    trust: TrustLevel
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_text(self.source_id, "source_id")
        _validate_text(self.source_type, "source_type")
        object.__setattr__(
            self, "occurred_at", _normalize_datetime(self.occurred_at, "occurred_at")
        )
        _validate_trust(self.trust)
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class RawEventDraft:
    event_id: str
    scope: Scope
    source: SourceRef
    content: str
    retention: RetentionPolicy
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_text(self.event_id, "event_id")
        if not isinstance(self.scope, Scope):
            raise ValidationError("scope", "must be a Scope")
        if not isinstance(self.source, SourceRef):
            raise ValidationError("source", "must be a SourceRef")
        _validate_content(self.content)
        if not isinstance(self.retention, RetentionPolicy):
            raise ValidationError("retention", "must be a RetentionPolicy")
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class MemoryDraft:
    memory_id: str
    scope: Scope
    content: str
    source_event_ids: tuple[str, ...]
    kind: str
    trust: TrustLevel
    created_at: datetime
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_text(self.memory_id, "memory_id")
        if not isinstance(self.scope, Scope):
            raise ValidationError("scope", "must be a Scope")
        _validate_content(self.content)
        _validate_source_event_ids(self.source_event_ids)
        _validate_text(self.kind, "kind")
        _validate_trust(self.trust)
        object.__setattr__(
            self, "created_at", _normalize_datetime(self.created_at, "created_at")
        )
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class RecallQuery:
    scope: Scope
    as_of: datetime
    limit: int = 20
    kinds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.scope, Scope):
            raise ValidationError("scope", "must be a Scope")
        object.__setattr__(self, "as_of", _normalize_datetime(self.as_of, "as_of"))
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= 100
        ):
            raise ValidationError("limit", "must be from 1 through 100")
        if not isinstance(self.kinds, tuple):
            raise ValidationError("kinds", "must be a tuple")
        for kind in self.kinds:
            _validate_text(kind, "kinds")


@dataclass(frozen=True, slots=True)
class GovernanceCommand:
    scope: Scope
    action: GovernanceAction
    entity_type: EntityType
    entity_id: str
    occurred_at: datetime
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, Scope):
            raise ValidationError("scope", "must be a Scope")
        if not isinstance(self.action, GovernanceAction):
            raise ValidationError("action", "must be a GovernanceAction")
        if not isinstance(self.entity_type, EntityType):
            raise ValidationError("entity_type", "must be an EntityType")
        _validate_text(self.entity_id, "entity_id")
        object.__setattr__(
            self, "occurred_at", _normalize_datetime(self.occurred_at, "occurred_at")
        )
        _validate_text(self.reason, "reason")


@dataclass(frozen=True, slots=True)
class RawEventReceipt:
    event_id: str
    scope: Scope
    source: SourceRef
    content_hash: str
    ingested_at: datetime
    raw_expires_at: datetime
    clear_for: timedelta
    recall_for: timedelta
    content_available: bool
    metadata: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        _validate_text(self.event_id, "event_id")
        if not isinstance(self.scope, Scope):
            raise ValidationError("scope", "must be a Scope")
        if not isinstance(self.source, SourceRef):
            raise ValidationError("source", "must be a SourceRef")
        _validate_text(self.content_hash, "content_hash")
        object.__setattr__(
            self, "ingested_at", _normalize_datetime(self.ingested_at, "ingested_at")
        )
        object.__setattr__(
            self,
            "raw_expires_at",
            _normalize_datetime(self.raw_expires_at, "raw_expires_at"),
        )
        _validate_positive_retention(self.clear_for, "clear_for")
        _validate_positive_retention(self.recall_for, "recall_for")
        if self.recall_for < self.clear_for:
            raise ValidationError("recall_for", "must be at least clear_for")
        if not isinstance(self.content_available, bool):
            raise ValidationError("content_available", "must be a bool")
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    memory_id: str
    scope: Scope
    content: str
    source_event_ids: tuple[str, ...]
    dependency_fingerprint: str
    kind: str
    trust: TrustLevel
    created_at: datetime
    clear_until: datetime
    recall_until: datetime
    status: MemoryStatus
    metadata: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        _validate_text(self.memory_id, "memory_id")
        if not isinstance(self.scope, Scope):
            raise ValidationError("scope", "must be a Scope")
        _validate_content(self.content)
        _validate_source_event_ids(self.source_event_ids)
        _validate_text(self.dependency_fingerprint, "dependency_fingerprint")
        _validate_text(self.kind, "kind")
        _validate_trust(self.trust)
        object.__setattr__(
            self, "created_at", _normalize_datetime(self.created_at, "created_at")
        )
        object.__setattr__(
            self, "clear_until", _normalize_datetime(self.clear_until, "clear_until")
        )
        object.__setattr__(
            self, "recall_until", _normalize_datetime(self.recall_until, "recall_until")
        )
        if self.recall_until < self.clear_until:
            raise ValidationError("recall_until", "must not precede clear_until")
        if not isinstance(self.status, MemoryStatus):
            raise ValidationError("status", "must be a MemoryStatus")
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class RecallItem:
    memory: MemoryRecord
    clarity: float

    def __post_init__(self) -> None:
        if not isinstance(self.memory, MemoryRecord):
            raise ValidationError("memory", "must be a MemoryRecord")
        if not isinstance(self.clarity, float) or not math.isfinite(self.clarity):
            raise ValidationError("clarity", "must be a finite float")


@dataclass(frozen=True, slots=True)
class GovernanceResult:
    command: GovernanceCommand
    affected_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.command, GovernanceCommand):
            raise ValidationError("command", "must be a GovernanceCommand")
        if not isinstance(self.affected_ids, tuple):
            raise ValidationError("affected_ids", "must be a tuple")
        for entity_id in self.affected_ids:
            _validate_text(entity_id, "affected_ids")


@dataclass(frozen=True, slots=True)
class AuditRecord:
    audit_id: str
    scope: Scope
    action: str
    entity_type: EntityType
    entity_id: str
    occurred_at: datetime
    reason: str
    metadata: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        _validate_text(self.audit_id, "audit_id")
        if not isinstance(self.scope, Scope):
            raise ValidationError("scope", "must be a Scope")
        _validate_text(self.action, "action")
        if not isinstance(self.entity_type, EntityType):
            raise ValidationError("entity_type", "must be an EntityType")
        _validate_text(self.entity_id, "entity_id")
        object.__setattr__(
            self, "occurred_at", _normalize_datetime(self.occurred_at, "occurred_at")
        )
        _validate_text(self.reason, "reason")
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))
