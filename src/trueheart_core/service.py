"""Public lifecycle service for TrueHeart Core."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256

from .domain import RawEventDraft, RawEventReceipt, _normalize_datetime
from .ports import Repository


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
