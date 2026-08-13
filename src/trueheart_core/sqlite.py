"""SQLite persistence adapter for TrueHeart Core."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

from .domain import (
    EntityType,
    JsonValue,
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

_SCHEMA_STATEMENTS = tuple(
    statement.strip() for statement in _SCHEMA.split(";") if statement.strip()
)
_WAL_RETRY_ATTEMPTS = 30
_WAL_RETRY_INITIAL_SECONDS = 0.002
_WAL_RETRY_MAX_SECONDS = 0.05


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
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 25")
            connection.execute("PRAGMA foreign_keys = ON")
            self._enable_wal(connection)
            connection.execute("PRAGMA busy_timeout = 5000")
            return connection
        except (RepositoryCorruption, sqlite3.Error):
            connection.close()
            raise

    @staticmethod
    def _enable_wal(connection: sqlite3.Connection) -> None:
        delay = _WAL_RETRY_INITIAL_SECONDS
        for attempt in range(_WAL_RETRY_ATTEMPTS):
            try:
                row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            except sqlite3.OperationalError as error:
                error_code = getattr(error, "sqlite_errorcode", None)
                base_error_code = (
                    error_code & 0xFF if isinstance(error_code, int) else None
                )
                retryable = base_error_code in (
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                )
                if not retryable or attempt + 1 == _WAL_RETRY_ATTEMPTS:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, _WAL_RETRY_MAX_SECONDS)
                continue
            if row is None or row[0] != "wal":
                raise RepositoryCorruption("WAL mode unavailable")
            return
        raise RepositoryCorruption("WAL mode unavailable")

    def _initialize_schema(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            self._require_supported_schema(connection)
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) "
                "VALUES (?, ?)",
                (SCHEMA_VERSION, _datetime_text(datetime.now(UTC))),
            )
            connection.commit()
        except RepositoryCorruption:
            if connection is not None:
                self._rollback_quietly(connection)
            raise
        except sqlite3.Error:
            if connection is not None:
                self._rollback_quietly(connection)
            raise RepositoryCorruption("schema initialization failed") from None
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _require_supported_schema(connection: sqlite3.Connection) -> None:
        migration_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?",
            ("table", "schema_migrations"),
        ).fetchone()
        if migration_table is None:
            return
        row = connection.execute(
            "SELECT MAX(version) AS version FROM schema_migrations"
        ).fetchone()
        if row is None or row["version"] is None:
            return
        try:
            version = int(row["version"])
        except (TypeError, ValueError):
            raise RepositoryCorruption("invalid schema version") from None
        if version > SCHEMA_VERSION:
            raise RepositoryCorruption("unsupported schema version")

    @staticmethod
    def _rollback_quietly(connection: sqlite3.Connection) -> None:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass

    def ingest_event(
        self,
        draft: RawEventDraft,
        *,
        content_hash: str,
        ingested_at: datetime,
        raw_expires_at: datetime,
    ) -> RawEventReceipt:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            self._require_supported_schema(connection)
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
                receipt = self._receipt(existing)
                if not self._is_identical(
                    receipt,
                    draft,
                    content_hash=content_hash,
                    raw_expires_at=raw_expires_at,
                ):
                    raise IdempotencyConflict(draft.event_id)
                connection.commit()
                return receipt

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
                raise RepositoryCorruption("inserted raw event missing")
            receipt = self._receipt(row)
            connection.commit()
            return receipt
        except (EntityDeleted, IdempotencyConflict, RepositoryCorruption):
            if connection is not None:
                self._rollback_quietly(connection)
            raise
        except sqlite3.Error:
            if connection is not None:
                self._rollback_quietly(connection)
            raise RepositoryCorruption("event ingest failed") from None
        except BaseException:
            if connection is not None:
                self._rollback_quietly(connection)
            raise
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _is_identical(
        receipt: RawEventReceipt,
        draft: RawEventDraft,
        *,
        content_hash: str,
        raw_expires_at: datetime,
    ) -> bool:
        return (
            receipt.scope == draft.scope
            and receipt.source == draft.source
            and receipt.content_hash == content_hash
            and receipt.raw_expires_at == raw_expires_at
            and receipt.clear_for == draft.retention.clear_for
            and receipt.recall_for == draft.retention.recall_for
            and receipt.metadata == draft.metadata
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
        try:
            source_occurred_at = SQLiteRepository._stored_datetime(
                row, "source_occurred_at"
            )
            ingested_at = SQLiteRepository._stored_datetime(row, "ingested_at")
            raw_expires_at = SQLiteRepository._stored_datetime(row, "raw_expires_at")
            source_metadata = SQLiteRepository._stored_metadata(
                row, "source_metadata_json"
            )
            event_metadata = SQLiteRepository._stored_metadata(
                row, "event_metadata_json"
            )
            content_hash = SQLiteRepository._stored_text(row, "content_hash")
            if len(content_hash) != 64 or any(
                character not in "0123456789abcdef" for character in content_hash
            ):
                raise ValueError
            if SQLiteRepository._stored_text(row, "status") != "active":
                raise ValueError
            content_available = row["content_available"]
            if type(content_available) is not int or content_available not in (0, 1):
                raise TypeError
            return RawEventReceipt(
                event_id=SQLiteRepository._stored_text(row, "event_id"),
                scope=Scope(
                    SQLiteRepository._stored_text(row, "tenant_id"),
                    SQLiteRepository._stored_text(row, "owner_id"),
                    SQLiteRepository._stored_text(row, "subject_id"),
                ),
                source=SourceRef(
                    source_id=SQLiteRepository._stored_text(row, "source_id"),
                    source_type=SQLiteRepository._stored_text(row, "source_type"),
                    occurred_at=source_occurred_at,
                    trust=TrustLevel(
                        SQLiteRepository._stored_integer(row, "source_trust")
                    ),
                    metadata=source_metadata,
                ),
                content_hash=content_hash,
                ingested_at=ingested_at,
                raw_expires_at=raw_expires_at,
                clear_for=timedelta(
                    microseconds=SQLiteRepository._stored_integer(
                        row, "clear_for_microseconds"
                    )
                ),
                recall_for=timedelta(
                    microseconds=SQLiteRepository._stored_integer(
                        row, "recall_for_microseconds"
                    )
                ),
                content_available=bool(content_available),
                metadata=event_metadata,
            )
        except (
            IndexError,
            KeyError,
            OverflowError,
            TypeError,
            ValueError,
            ValidationError,
        ):
            raise RepositoryCorruption("invalid raw event record") from None

    @staticmethod
    def _stored_text(row: sqlite3.Row, key: str) -> str:
        value = row[key]
        if type(value) is not str:
            raise TypeError
        return value

    @staticmethod
    def _stored_integer(row: sqlite3.Row, key: str) -> int:
        value = row[key]
        if type(value) is not int:
            raise TypeError
        return value

    @staticmethod
    def _stored_datetime(row: sqlite3.Row, key: str) -> datetime:
        text = SQLiteRepository._stored_text(row, key)
        value = datetime.fromisoformat(text)
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError
        normalized = value.astimezone(UTC)
        if _datetime_text(normalized) != text:
            raise ValueError
        return normalized

    @staticmethod
    def _stored_metadata(row: sqlite3.Row, key: str) -> dict[str, JsonValue]:
        text = SQLiteRepository._stored_text(row, key)
        value = json.loads(text)
        if not isinstance(value, dict) or _canonical_json(value) != text:
            raise ValueError
        return cast(dict[str, JsonValue], value)
