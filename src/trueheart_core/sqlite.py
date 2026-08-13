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
    MemoryDraft,
    MemoryRecord,
    MemoryStatus,
    RawEventDraft,
    RawEventReceipt,
    Scope,
    SourceRef,
    TrustLevel,
    _canonical_json,
)
from .errors import (
    EntityDeleted,
    EntityNotFound,
    IdempotencyConflict,
    RepositoryCorruption,
    ScopeMismatch,
    TrustEscalation,
    ValidationError,
)
from .ports import _dependency_fingerprint

SCHEMA_VERSION = 1
_CORE_TABLES = frozenset(
    {
        "raw_events",
        "raw_event_content",
        "memories",
        "memory_sources",
        "tombstones",
        "audit_log",
    }
)

_CURRENT_MEMORY_SOURCES_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_sources (
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    source_trust_snapshot INTEGER NOT NULL
        CHECK (source_trust_snapshot BETWEEN 0 AND 2),
    clear_for_microseconds_snapshot INTEGER NOT NULL
        CHECK (clear_for_microseconds_snapshot > 0),
    recall_for_microseconds_snapshot INTEGER NOT NULL
        CHECK (recall_for_microseconds_snapshot >= clear_for_microseconds_snapshot),
    PRIMARY KEY (tenant_id, owner_id, subject_id, memory_id, event_id),
    FOREIGN KEY (tenant_id, owner_id, subject_id, memory_id)
        REFERENCES memories (tenant_id, owner_id, subject_id, memory_id)
        ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, owner_id, subject_id, event_id)
        REFERENCES raw_events (tenant_id, owner_id, subject_id, event_id)
)
""".strip()

_LEGACY_MEMORY_SOURCES_SCHEMA = """
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
)
""".strip()

_MEMORY_SOURCES_INDEX_SCHEMA = (
    "CREATE INDEX IF NOT EXISTS idx_memory_sources_event "
    "ON memory_sources (tenant_id, owner_id, subject_id, event_id)"
)

_SCHEMA = f"""
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

{_CURRENT_MEMORY_SOURCES_SCHEMA};

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
{_MEMORY_SOURCES_INDEX_SCHEMA};
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
            fresh_schema = self._requires_fresh_schema(connection)
            if not fresh_schema:
                self._migrate_legacy_memory_sources(connection)
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            if fresh_schema:
                self._migrate_legacy_memory_sources(connection)
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

    @classmethod
    def _requires_fresh_schema(cls, connection: sqlite3.Connection) -> bool:
        table_rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = ?",
            ("table",),
        ).fetchall()
        try:
            table_names = {
                cls._stored_text(table_row, "name") for table_row in table_rows
            }
        except (IndexError, KeyError, TypeError, ValueError):
            raise RepositoryCorruption("invalid schema catalog") from None
        if "schema_migrations" not in table_names:
            if table_names & _CORE_TABLES:
                raise RepositoryCorruption("incomplete schema")
            return True

        cls._require_supported_schema(connection)
        version_rows = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        if not version_rows:
            if table_names & _CORE_TABLES:
                raise RepositoryCorruption("incomplete schema")
            return True
        try:
            versions = tuple(
                cls._stored_integer(version_row, "version")
                for version_row in version_rows
            )
        except (IndexError, KeyError, TypeError, ValueError):
            raise RepositoryCorruption("invalid schema version") from None
        if versions != (SCHEMA_VERSION,):
            raise RepositoryCorruption("invalid schema version")
        if not _CORE_TABLES.issubset(table_names):
            raise RepositoryCorruption("incomplete schema")
        return False

    @staticmethod
    def _normalized_schema_sql(value: str) -> str:
        normalized = " ".join(value.replace("IF NOT EXISTS ", "").split())
        return normalized.replace("( ", "(").replace(" )", ")").removesuffix(";")

    @classmethod
    def _migrate_legacy_memory_sources(cls, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
            ("table", "memory_sources"),
        ).fetchone()
        if row is None or type(row["sql"]) is not str:
            raise RepositoryCorruption("invalid memory_sources schema")
        stored_schema = cls._normalized_schema_sql(row["sql"])
        current_schema = cls._normalized_schema_sql(_CURRENT_MEMORY_SOURCES_SCHEMA)
        if stored_schema == current_schema:
            return
        legacy_schema = cls._normalized_schema_sql(_LEGACY_MEMORY_SOURCES_SCHEMA)
        if stored_schema != legacy_schema:
            raise RepositoryCorruption("unsupported memory_sources schema")

        count_row = connection.execute(
            "SELECT COUNT(*) AS row_count FROM memory_sources"
        ).fetchone()
        if count_row is None or type(count_row["row_count"]) is not int:
            raise RepositoryCorruption("legacy lineage migration failed")
        legacy_count = count_row["row_count"]
        connection.execute("DROP INDEX IF EXISTS idx_memory_sources_event")
        connection.execute(
            "ALTER TABLE memory_sources RENAME TO memory_sources_legacy_v1"
        )
        connection.execute(_CURRENT_MEMORY_SOURCES_SCHEMA)
        connection.execute(
            "INSERT INTO memory_sources (tenant_id, owner_id, subject_id, "
            "memory_id, event_id, source_trust_snapshot, "
            "clear_for_microseconds_snapshot, recall_for_microseconds_snapshot) "
            "SELECT legacy.tenant_id, legacy.owner_id, legacy.subject_id, "
            "legacy.memory_id, legacy.event_id, receipts.source_trust, "
            "receipts.clear_for_microseconds, receipts.recall_for_microseconds "
            "FROM memory_sources_legacy_v1 AS legacy "
            "JOIN raw_events AS receipts ON receipts.tenant_id = legacy.tenant_id "
            "AND receipts.owner_id = legacy.owner_id "
            "AND receipts.subject_id = legacy.subject_id "
            "AND receipts.event_id = legacy.event_id"
        )
        migrated_count_row = connection.execute(
            "SELECT COUNT(*) AS row_count FROM memory_sources"
        ).fetchone()
        if (
            migrated_count_row is None
            or type(migrated_count_row["row_count"]) is not int
            or migrated_count_row["row_count"] != legacy_count
        ):
            raise RepositoryCorruption("legacy lineage migration failed")
        connection.execute("DROP TABLE memory_sources_legacy_v1")
        connection.execute(_MEMORY_SOURCES_INDEX_SCHEMA)

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

    def materialize_once(
        self,
        draft: MemoryDraft,
        *,
        dependency_fingerprint: str,
    ) -> MemoryRecord:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            self._require_supported_schema(connection)
            scope = _scope_values(draft.scope)

            identifier_tombstone = connection.execute(
                "SELECT 1 FROM tombstones WHERE tenant_id = ? AND owner_id = ? "
                "AND subject_id = ? AND entity_type = ? AND entity_id = ?",
                (*scope, EntityType.MEMORY.value, draft.memory_id),
            ).fetchone()
            if identifier_tombstone is not None:
                raise EntityDeleted(draft.memory_id)
            fingerprint_tombstone = connection.execute(
                "SELECT 1 FROM tombstones WHERE tenant_id = ? AND owner_id = ? "
                "AND subject_id = ? AND entity_type = ? "
                "AND dependency_fingerprint = ?",
                (*scope, EntityType.MEMORY.value, dependency_fingerprint),
            ).fetchone()
            if fingerprint_tombstone is not None:
                raise EntityDeleted(draft.memory_id)

            existing = connection.execute(
                "SELECT * FROM memories WHERE tenant_id = ? AND owner_id = ? "
                "AND subject_id = ? AND memory_id = ?",
                (*scope, draft.memory_id),
            ).fetchone()
            if existing is not None:
                source_rows = connection.execute(
                    "SELECT event_id, event_id AS receipt_event_id, "
                    "source_trust_snapshot, clear_for_microseconds_snapshot, "
                    "recall_for_microseconds_snapshot FROM memory_sources "
                    "WHERE tenant_id = ? AND owner_id = ? AND subject_id = ? "
                    "AND memory_id = ? ORDER BY event_id",
                    (*scope, draft.memory_id),
                ).fetchall()
                record = self._memory_record(existing, source_rows)
                if not self._is_identical_memory(
                    record,
                    draft,
                    dependency_fingerprint=dependency_fingerprint,
                ):
                    raise IdempotencyConflict(draft.memory_id)
                connection.commit()
                return record

            source_receipts: list[RawEventReceipt] = []
            for event_id in draft.source_event_ids:
                row = connection.execute(
                    "SELECT raw_events.*, "
                    "EXISTS(SELECT 1 FROM raw_event_content AS content "
                    "WHERE content.tenant_id = raw_events.tenant_id "
                    "AND content.owner_id = raw_events.owner_id "
                    "AND content.subject_id = raw_events.subject_id "
                    "AND content.event_id = raw_events.event_id) AS content_available "
                    "FROM raw_events WHERE tenant_id = ? AND owner_id = ? "
                    "AND subject_id = ? AND event_id = ?",
                    (*scope, event_id),
                ).fetchone()
                if row is None:
                    deleted = connection.execute(
                        "SELECT 1 FROM tombstones WHERE tenant_id = ? "
                        "AND owner_id = ? AND subject_id = ? AND entity_type = ? "
                        "AND entity_id = ?",
                        (*scope, EntityType.RAW_EVENT.value, event_id),
                    ).fetchone()
                    if deleted is not None:
                        raise EntityDeleted(event_id)
                    other_scope = connection.execute(
                        "SELECT 1 FROM raw_events WHERE event_id = ? LIMIT 1",
                        (event_id,),
                    ).fetchone()
                    if other_scope is not None:
                        raise ScopeMismatch(event_id)
                    raise EntityNotFound(event_id)
                source_receipts.append(self._receipt(row))

            minimum_trust = min(receipt.source.trust for receipt in source_receipts)
            if draft.trust > minimum_trust:
                raise TrustEscalation(draft.memory_id)
            shortest_clear_for = min(receipt.clear_for for receipt in source_receipts)
            shortest_recall_for = min(receipt.recall_for for receipt in source_receipts)
            clear_until = draft.created_at + shortest_clear_for
            recall_until = draft.created_at + shortest_recall_for

            connection.execute(
                "INSERT INTO memories (tenant_id, owner_id, subject_id, memory_id, "
                "content, dependency_fingerprint, kind, trust, created_at, "
                "clear_until, recall_until, status, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    *scope,
                    draft.memory_id,
                    draft.content,
                    dependency_fingerprint,
                    draft.kind,
                    int(draft.trust),
                    _datetime_text(draft.created_at),
                    _datetime_text(clear_until),
                    _datetime_text(recall_until),
                    MemoryStatus.ACTIVE.value,
                    _canonical_json(draft.metadata),
                ),
            )
            receipts_by_id = {receipt.event_id: receipt for receipt in source_receipts}
            for event_id in sorted(draft.source_event_ids):
                receipt = receipts_by_id[event_id]
                connection.execute(
                    "INSERT INTO memory_sources (tenant_id, owner_id, subject_id, "
                    "memory_id, event_id, source_trust_snapshot, "
                    "clear_for_microseconds_snapshot, "
                    "recall_for_microseconds_snapshot) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        *scope,
                        draft.memory_id,
                        event_id,
                        int(receipt.source.trust),
                        _timedelta_microseconds(receipt.clear_for),
                        _timedelta_microseconds(receipt.recall_for),
                    ),
                )
            self._insert_materialize_audit(connection, draft)
            inserted = connection.execute(
                "SELECT * FROM memories WHERE tenant_id = ? AND owner_id = ? "
                "AND subject_id = ? AND memory_id = ?",
                (*scope, draft.memory_id),
            ).fetchone()
            if inserted is None:
                raise RepositoryCorruption("inserted memory missing")
            inserted_sources = connection.execute(
                "SELECT event_id, event_id AS receipt_event_id, "
                "source_trust_snapshot, clear_for_microseconds_snapshot, "
                "recall_for_microseconds_snapshot FROM memory_sources "
                "WHERE tenant_id = ? AND owner_id = ? AND subject_id = ? "
                "AND memory_id = ? ORDER BY event_id",
                (*scope, draft.memory_id),
            ).fetchall()
            record = self._memory_record(inserted, inserted_sources)
            connection.commit()
            return record
        except (
            EntityDeleted,
            EntityNotFound,
            IdempotencyConflict,
            RepositoryCorruption,
            ScopeMismatch,
            TrustEscalation,
        ):
            if connection is not None:
                self._rollback_quietly(connection)
            raise
        except sqlite3.Error:
            if connection is not None:
                self._rollback_quietly(connection)
            raise RepositoryCorruption("memory materialization failed") from None
        except BaseException:
            if connection is not None:
                self._rollback_quietly(connection)
            raise
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _is_identical_memory(
        record: MemoryRecord,
        draft: MemoryDraft,
        *,
        dependency_fingerprint: str,
    ) -> bool:
        return (
            record.memory_id == draft.memory_id
            and record.scope == draft.scope
            and record.content == draft.content
            and record.source_event_ids == tuple(sorted(draft.source_event_ids))
            and record.dependency_fingerprint == dependency_fingerprint
            and record.kind == draft.kind
            and record.trust == draft.trust
            and record.created_at == draft.created_at
            and record.metadata == draft.metadata
        )

    @staticmethod
    def _insert_materialize_audit(
        connection: sqlite3.Connection, draft: MemoryDraft
    ) -> None:
        connection.execute(
            "INSERT INTO audit_log (audit_id, tenant_id, owner_id, subject_id, "
            "action, entity_type, entity_id, occurred_at, reason, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                *_scope_values(draft.scope),
                "materialize",
                EntityType.MEMORY.value,
                draft.memory_id,
                _datetime_text(draft.created_at),
                "memory materialized",
                "{}",
            ),
        )

    @staticmethod
    def _memory_record(
        row: sqlite3.Row, source_rows: list[sqlite3.Row]
    ) -> MemoryRecord:
        try:
            scope = Scope(
                SQLiteRepository._stored_text(row, "tenant_id"),
                SQLiteRepository._stored_text(row, "owner_id"),
                SQLiteRepository._stored_text(row, "subject_id"),
            )
            memory_id = SQLiteRepository._stored_text(row, "memory_id")
            source_event_ids_list: list[str] = []
            source_trusts: list[TrustLevel] = []
            source_clear_periods: list[timedelta] = []
            source_recall_periods: list[timedelta] = []
            for source_row in source_rows:
                event_id = SQLiteRepository._stored_text(source_row, "event_id")
                if (
                    SQLiteRepository._stored_text(source_row, "receipt_event_id")
                    != event_id
                ):
                    raise ValueError
                source_event_ids_list.append(event_id)
                source_trusts.append(
                    TrustLevel(
                        SQLiteRepository._stored_integer(
                            source_row, "source_trust_snapshot"
                        )
                    )
                )
                clear_period = timedelta(
                    microseconds=SQLiteRepository._stored_integer(
                        source_row, "clear_for_microseconds_snapshot"
                    )
                )
                recall_period = timedelta(
                    microseconds=SQLiteRepository._stored_integer(
                        source_row, "recall_for_microseconds_snapshot"
                    )
                )
                if clear_period <= timedelta(0) or recall_period < clear_period:
                    raise ValueError
                source_clear_periods.append(clear_period)
                source_recall_periods.append(recall_period)
            source_event_ids = tuple(source_event_ids_list)
            kind = SQLiteRepository._stored_text(row, "kind")
            dependency_fingerprint = SQLiteRepository._stored_text(
                row, "dependency_fingerprint"
            )
            if dependency_fingerprint != _dependency_fingerprint(
                scope, kind, source_event_ids
            ):
                raise ValueError
            trust = TrustLevel(SQLiteRepository._stored_integer(row, "trust"))
            created_at = SQLiteRepository._stored_datetime(row, "created_at")
            clear_until = SQLiteRepository._stored_datetime(row, "clear_until")
            recall_until = SQLiteRepository._stored_datetime(row, "recall_until")
            if trust > min(source_trusts):
                raise ValueError
            if clear_until != created_at + min(source_clear_periods):
                raise ValueError
            if recall_until != created_at + min(source_recall_periods):
                raise ValueError
            return MemoryRecord(
                memory_id=memory_id,
                scope=scope,
                content=SQLiteRepository._stored_text(row, "content"),
                source_event_ids=source_event_ids,
                dependency_fingerprint=dependency_fingerprint,
                kind=kind,
                trust=trust,
                created_at=created_at,
                clear_until=clear_until,
                recall_until=recall_until,
                status=MemoryStatus(SQLiteRepository._stored_text(row, "status")),
                metadata=SQLiteRepository._stored_metadata(row, "metadata_json"),
            )
        except (
            IndexError,
            KeyError,
            OverflowError,
            TypeError,
            ValueError,
            ValidationError,
        ):
            raise RepositoryCorruption("invalid memory record") from None

    def recall_candidates(
        self,
        scope: Scope,
        *,
        as_of: datetime,
        kinds: tuple[str, ...],
    ) -> tuple[MemoryRecord, ...]:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN")
            scope_values = _scope_values(scope)
            rows = connection.execute(
                "SELECT * FROM memories WHERE tenant_id = ? AND owner_id = ? "
                "AND subject_id = ?",
                scope_values,
            ).fetchall()
            source_rows = connection.execute(
                "SELECT edges.memory_id, edges.event_id, "
                "receipts.event_id AS receipt_event_id, "
                "edges.source_trust_snapshot, "
                "edges.clear_for_microseconds_snapshot, "
                "edges.recall_for_microseconds_snapshot "
                "FROM memory_sources AS edges LEFT JOIN raw_events AS receipts "
                "ON receipts.tenant_id = edges.tenant_id "
                "AND receipts.owner_id = edges.owner_id "
                "AND receipts.subject_id = edges.subject_id "
                "AND receipts.event_id = edges.event_id "
                "WHERE edges.tenant_id = ? AND edges.owner_id = ? "
                "AND edges.subject_id = ? ORDER BY edges.memory_id, edges.event_id",
                scope_values,
            ).fetchall()
            try:
                sources_by_memory: dict[str, list[sqlite3.Row]] = {}
                for source_row in source_rows:
                    memory_id = self._stored_text(source_row, "memory_id")
                    sources_by_memory.setdefault(memory_id, []).append(source_row)
                records: list[MemoryRecord] = []
                memory_ids: set[str] = set()
                for row in rows:
                    memory_id = self._stored_text(row, "memory_id")
                    memory_ids.add(memory_id)
                    records.append(
                        self._memory_record(row, sources_by_memory.get(memory_id, []))
                    )
            except (IndexError, KeyError, TypeError, ValueError, ValidationError):
                raise RepositoryCorruption("invalid memory record") from None
            if set(sources_by_memory) != memory_ids:
                raise RepositoryCorruption("invalid memory record")
            eligible = tuple(
                record
                for record in records
                if record.status is MemoryStatus.ACTIVE
                and record.recall_until > as_of
                and (not kinds or record.kind in kinds)
            )
            connection.commit()
            return eligible
        except RepositoryCorruption:
            if connection is not None:
                self._rollback_quietly(connection)
            raise
        except sqlite3.Error:
            if connection is not None:
                self._rollback_quietly(connection)
            raise RepositoryCorruption("memory recall failed") from None
        except BaseException:
            if connection is not None:
                self._rollback_quietly(connection)
            raise
        finally:
            if connection is not None:
                connection.close()

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
