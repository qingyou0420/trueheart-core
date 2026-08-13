"""SQLite persistence adapter for TrueHeart Core."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from .domain import (
    EntityType,
    RawEventDraft,
    RawEventReceipt,
    Scope,
    SourceRef,
    TrustLevel,
    _canonical_json,
)
from .errors import (
    EntityDeleted,
    IdempotencyConflict,
    RepositoryCorruption,
    ValidationError,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_events (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_occurred_at TEXT NOT NULL,
    source_trust INTEGER NOT NULL CHECK (source_trust BETWEEN 0 AND 2),
    source_metadata_json TEXT NOT NULL CHECK (json_valid(source_metadata_json)),
    content_hash TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    raw_expires_at TEXT NOT NULL,
    clear_for_microseconds INTEGER NOT NULL CHECK (clear_for_microseconds > 0),
    recall_for_microseconds INTEGER NOT NULL CHECK (recall_for_microseconds > 0),
    event_metadata_json TEXT NOT NULL CHECK (json_valid(event_metadata_json)),
    status TEXT NOT NULL DEFAULT 'active',
    PRIMARY KEY (tenant_id, owner_id, subject_id, event_id)
);

CREATE TABLE IF NOT EXISTS raw_event_content (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (tenant_id, owner_id, subject_id, event_id),
    FOREIGN KEY (tenant_id, owner_id, subject_id, event_id)
        REFERENCES raw_events (tenant_id, owner_id, subject_id, event_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS memories (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    content TEXT NOT NULL,
    dependency_fingerprint TEXT NOT NULL,
    kind TEXT NOT NULL,
    trust INTEGER NOT NULL CHECK (trust BETWEEN 0 AND 2),
    created_at TEXT NOT NULL,
    clear_until TEXT NOT NULL,
    recall_until TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'sealed')),
    metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json)),
    PRIMARY KEY (tenant_id, owner_id, subject_id, memory_id)
);

CREATE TABLE IF NOT EXISTS memory_sources (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    PRIMARY KEY (tenant_id, owner_id, subject_id, memory_id, event_id),
    FOREIGN KEY (tenant_id, owner_id, subject_id, memory_id)
        REFERENCES memories (tenant_id, owner_id, subject_id, memory_id)
        ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, owner_id, subject_id, event_id)
        REFERENCES raw_events (tenant_id, owner_id, subject_id, event_id)
);

CREATE TABLE IF NOT EXISTS tombstones (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('raw_event', 'memory')),
    entity_id TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    dependency_fingerprint TEXT,
    metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json)),
    PRIMARY KEY (tenant_id, owner_id, subject_id, entity_type, entity_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('raw_event', 'memory')),
    entity_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json))
);

CREATE INDEX IF NOT EXISTS idx_audit_scope_time
    ON audit_log (tenant_id, owner_id, subject_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_memory_sources_event
    ON memory_sources (tenant_id, owner_id, subject_id, event_id);
"""


def _datetime_text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _timedelta_microseconds(value: timedelta) -> int:
    return value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds


def _scope_values(scope: Scope) -> tuple[str, str, str]:
    return scope.tenant_id, scope.owner_id, scope.subject_id


class SQLiteRepository:
    def __init__(self, path: str | Path) -> None:
        if str(path) == ":memory:":
            raise ValidationError("path", "must be a file-backed database")
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
            migration_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?",
                ("table", "schema_migrations"),
            ).fetchone()
            if migration_table is not None:
                row = connection.execute(
                    "SELECT MAX(version) AS version FROM schema_migrations"
                ).fetchone()
                if row is not None and row["version"] is not None:
                    version = int(row["version"])
                    if version > SCHEMA_VERSION:
                        raise RepositoryCorruption("unsupported schema version")
            connection.executescript(_SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) "
                "VALUES (?, ?)",
                (SCHEMA_VERSION, _datetime_text(datetime.now().astimezone())),
            )

    def ingest_event(
        self,
        draft: RawEventDraft,
        *,
        content_hash: str,
        ingested_at: datetime,
        raw_expires_at: datetime,
    ) -> RawEventReceipt:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            tombstone = connection.execute(
                "SELECT 1 FROM tombstones WHERE tenant_id = ? AND owner_id = ? "
                "AND subject_id = ? AND entity_type = ? AND entity_id = ?",
                (
                    *_scope_values(draft.scope),
                    EntityType.RAW_EVENT.value,
                    draft.event_id,
                ),
            ).fetchone()
            if tombstone is not None:
                raise EntityDeleted(draft.event_id)
            existing = connection.execute(
                "SELECT raw_events.*, "
                "EXISTS(SELECT 1 FROM raw_event_content AS content "
                "WHERE content.tenant_id = raw_events.tenant_id "
                "AND content.owner_id = raw_events.owner_id "
                "AND content.subject_id = raw_events.subject_id "
                "AND content.event_id = raw_events.event_id) AS content_available "
                "FROM raw_events WHERE tenant_id = ? AND owner_id = ? "
                "AND subject_id = ? AND event_id = ?",
                (*_scope_values(draft.scope), draft.event_id),
            ).fetchone()
            if existing is not None:
                if not self._is_identical(
                    existing,
                    draft,
                    content_hash=content_hash,
                    raw_expires_at=raw_expires_at,
                ):
                    raise IdempotencyConflict(draft.event_id)
                connection.commit()
                return self._receipt(existing)

            scope = _scope_values(draft.scope)
            self._insert_audit(connection, draft, ingested_at)
            connection.execute(
                "INSERT INTO raw_events ("
                "tenant_id, owner_id, subject_id, event_id, source_id, source_type, "
                "source_occurred_at, source_trust, source_metadata_json, content_hash, "
                "ingested_at, raw_expires_at, clear_for_microseconds, "
                "recall_for_microseconds, event_metadata_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    *scope,
                    draft.event_id,
                    draft.source.source_id,
                    draft.source.source_type,
                    _datetime_text(draft.source.occurred_at),
                    int(draft.source.trust),
                    _canonical_json(draft.source.metadata),
                    content_hash,
                    _datetime_text(ingested_at),
                    _datetime_text(raw_expires_at),
                    _timedelta_microseconds(draft.retention.clear_for),
                    _timedelta_microseconds(draft.retention.recall_for),
                    _canonical_json(draft.metadata),
                ),
            )
            connection.execute(
                "INSERT INTO raw_event_content ("
                "tenant_id, owner_id, subject_id, event_id, content"
                ") VALUES (?, ?, ?, ?, ?)",
                (*scope, draft.event_id, draft.content),
            )
            row = connection.execute(
                "SELECT raw_events.*, 1 AS content_available FROM raw_events "
                "WHERE tenant_id = ? AND owner_id = ? AND subject_id = ? "
                "AND event_id = ?",
                (*scope, draft.event_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("inserted raw event is missing")
            connection.commit()
            return self._receipt(row)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _is_identical(
        row: sqlite3.Row,
        draft: RawEventDraft,
        *,
        content_hash: str,
        raw_expires_at: datetime,
    ) -> bool:
        return (
            tuple(str(row[key]) for key in ("tenant_id", "owner_id", "subject_id"))
            == _scope_values(draft.scope)
            and str(row["source_id"]) == draft.source.source_id
            and str(row["source_type"]) == draft.source.source_type
            and str(row["source_occurred_at"])
            == _datetime_text(draft.source.occurred_at)
            and int(row["source_trust"]) == int(draft.source.trust)
            and str(row["source_metadata_json"])
            == _canonical_json(draft.source.metadata)
            and str(row["content_hash"]) == content_hash
            and str(row["raw_expires_at"]) == _datetime_text(raw_expires_at)
            and int(row["clear_for_microseconds"])
            == _timedelta_microseconds(draft.retention.clear_for)
            and int(row["recall_for_microseconds"])
            == _timedelta_microseconds(draft.retention.recall_for)
            and str(row["event_metadata_json"]) == _canonical_json(draft.metadata)
        )

    @staticmethod
    def _insert_audit(
        connection: sqlite3.Connection,
        draft: RawEventDraft,
        ingested_at: datetime,
    ) -> None:
        connection.execute(
            "INSERT INTO audit_log ("
            "audit_id, tenant_id, owner_id, subject_id, action, entity_type, "
            "entity_id, occurred_at, reason, metadata_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                *_scope_values(draft.scope),
                "ingest",
                EntityType.RAW_EVENT.value,
                draft.event_id,
                _datetime_text(ingested_at),
                "event ingested",
                "{}",
            ),
        )

    @staticmethod
    def _receipt(row: sqlite3.Row) -> RawEventReceipt:
        return RawEventReceipt(
            event_id=str(row["event_id"]),
            scope=Scope(
                str(row["tenant_id"]),
                str(row["owner_id"]),
                str(row["subject_id"]),
            ),
            source=SourceRef(
                source_id=str(row["source_id"]),
                source_type=str(row["source_type"]),
                occurred_at=datetime.fromisoformat(str(row["source_occurred_at"])),
                trust=TrustLevel(int(row["source_trust"])),
                metadata=json.loads(str(row["source_metadata_json"])),
            ),
            content_hash=str(row["content_hash"]),
            ingested_at=datetime.fromisoformat(str(row["ingested_at"])),
            raw_expires_at=datetime.fromisoformat(str(row["raw_expires_at"])),
            clear_for=timedelta(microseconds=int(row["clear_for_microseconds"])),
            recall_for=timedelta(microseconds=int(row["recall_for_microseconds"])),
            content_available=bool(row["content_available"]),
            metadata=json.loads(str(row["event_metadata_json"])),
        )
