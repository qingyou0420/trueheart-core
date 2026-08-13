"""Public lifecycle service for TrueHeart Core."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256

from .domain import (
    AuditRecord,
    EntityType,
    GovernanceAction,
    GovernanceCommand,
    GovernanceResult,
    MemoryDraft,
    MemoryRecord,
    RawEventDraft,
    RawEventReceipt,
    RecallItem,
    RecallQuery,
    Scope,
    _normalize_datetime,
)
from .errors import ValidationError
from .ports import Repository, _dependency_fingerprint


def _clarity(memory: MemoryRecord, as_of: datetime) -> float:
    if as_of <= memory.clear_until:
        return 1.0
    fading_window = memory.recall_until - memory.clear_until
    remaining = memory.recall_until - as_of
    return remaining / fading_window


class TrueHeart:
    def __init__(
        self,
        repository: Repository,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._clock = clock

    def ingest_event(self, draft: RawEventDraft) -> RawEventReceipt:
        ingested_at = _normalize_datetime(self._clock(), "clock")
        content_hash = sha256(draft.content.encode("utf-8")).hexdigest()
        raw_expires_at = draft.source.occurred_at + draft.retention.raw_ttl
        return self._repository.ingest_event(
            draft,
            content_hash=content_hash,
            ingested_at=ingested_at,
            raw_expires_at=raw_expires_at,
        )

    def materialize_once(self, draft: MemoryDraft) -> MemoryRecord:
        dependency_fingerprint = _dependency_fingerprint(
            draft.scope, draft.kind, draft.source_event_ids
        )
        return self._repository.materialize_once(
            draft,
            dependency_fingerprint=dependency_fingerprint,
        )

    def recall(self, query: RecallQuery) -> tuple[RecallItem, ...]:
        candidates = self._repository.recall_candidates(
            query.scope,
            as_of=query.as_of,
            kinds=query.kinds,
        )
        items = [
            RecallItem(memory=memory, clarity=_clarity(memory, query.as_of))
            for memory in candidates
        ]
        items.sort(key=lambda item: item.memory.memory_id)
        items.sort(key=lambda item: item.memory.created_at, reverse=True)
        items.sort(key=lambda item: item.clarity, reverse=True)
        return tuple(items[: query.limit])

    def expire_raw_content(self, *, as_of: datetime) -> int:
        normalized_as_of = _normalize_datetime(as_of, "as_of")
        return self._repository.expire_raw_content(as_of=normalized_as_of)

    def govern(self, command: GovernanceCommand) -> GovernanceResult:
        if not isinstance(command, GovernanceCommand):
            raise ValidationError("command", "must be a GovernanceCommand")
        if (
            command.entity_type is EntityType.RAW_EVENT
            and command.action is not GovernanceAction.DELETE
        ):
            raise ValidationError("action", "is not supported for raw events")
        return self._repository.govern(command)

    def audit(self, scope: Scope, *, limit: int = 100) -> tuple[AuditRecord, ...]:
        if not isinstance(scope, Scope):
            raise ValidationError("scope", "must be a Scope")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValidationError("limit", "must be from 1 through 100")
        return self._repository.audit(scope, limit=limit)
