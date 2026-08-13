"""Persistence boundary for TrueHeart lifecycle operations."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .domain import (
    AuditRecord,
    GovernanceCommand,
    GovernanceResult,
    MemoryDraft,
    MemoryRecord,
    RawEventDraft,
    RawEventReceipt,
    Scope,
)


class Repository(Protocol):
    def ingest_event(
        self,
        draft: RawEventDraft,
        *,
        content_hash: str,
        ingested_at: datetime,
        raw_expires_at: datetime,
    ) -> RawEventReceipt: ...

    def materialize_once(
        self,
        draft: MemoryDraft,
        *,
        dependency_fingerprint: str,
    ) -> MemoryRecord: ...

    def recall_candidates(
        self,
        scope: Scope,
        *,
        as_of: datetime,
        kinds: tuple[str, ...],
    ) -> tuple[MemoryRecord, ...]: ...

    def expire_raw_content(self, *, as_of: datetime) -> int: ...

    def govern(self, command: GovernanceCommand) -> GovernanceResult: ...

    def audit(self, scope: Scope, *, limit: int) -> tuple[AuditRecord, ...]: ...
