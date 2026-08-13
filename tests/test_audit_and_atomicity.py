import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trueheart_core import (
    EntityType,
    GovernanceAction,
    GovernanceCommand,
    MemoryDraft,
    RawEventDraft,
    RecallQuery,
    RepositoryCorruption,
    RetentionPolicy,
    Scope,
    SourceRef,
    SQLiteRepository,
    TrueHeart,
    TrustLevel,
    ValidationError,
)

SCOPE = Scope("tenant", "owner", "subject")
OTHER_SCOPE = Scope("tenant", "owner", "other-subject")
BASE = datetime(2026, 8, 13, 10, tzinfo=UTC)
BODY_SENTINEL = "PRIVATE-BODY-SENTINEL-7f927"
SAFE_REASON = "safe lifecycle rationale"


def _service(path: Path) -> TrueHeart:
    return TrueHeart(SQLiteRepository(path), clock=lambda: BASE)


def _event(
    event_id: str,
    *,
    scope: Scope = SCOPE,
    occurred_at: datetime = BASE - timedelta(hours=1),
    raw_ttl: timedelta = timedelta(days=1),
) -> RawEventDraft:
    return RawEventDraft(
        event_id=event_id,
        scope=scope,
        source=SourceRef(
            source_id=f"source-{event_id}",
            source_type="synthetic",
            occurred_at=occurred_at,
            trust=TrustLevel.OBSERVED,
        ),
        content=f"event {event_id} {BODY_SENTINEL}",
        retention=RetentionPolicy(
            raw_ttl=raw_ttl,
            clear_for=timedelta(days=2),
            recall_for=timedelta(days=6),
        ),
    )


def _memory(
    memory_id: str,
    event_ids: tuple[str, ...],
    *,
    scope: Scope = SCOPE,
) -> MemoryDraft:
    return MemoryDraft(
        memory_id=memory_id,
        scope=scope,
        content=f"memory {memory_id} {BODY_SENTINEL}",
        source_event_ids=event_ids,
        kind="fact",
        trust=TrustLevel.OBSERVED,
        created_at=BASE,
    )


def _command(
    action: GovernanceAction,
    entity_type: EntityType,
    entity_id: str,
    *,
    scope: Scope = SCOPE,
    seconds: int,
) -> GovernanceCommand:
    return GovernanceCommand(
        scope=scope,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        occurred_at=BASE + timedelta(seconds=seconds),
        reason=SAFE_REASON,
    )


def _add_memory(
    service: TrueHeart,
    memory_id: str,
    event_id: str,
    *,
    scope: Scope = SCOPE,
) -> None:
    service.ingest_event(_event(event_id, scope=scope))
    service.materialize_once(_memory(memory_id, (event_id,), scope=scope))


def test_audit_is_exact_scope_stable_newest_first_and_validates_limit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "audit-order.db"
    service = _service(path)
    _add_memory(service, "mem-local", "evt-local")
    _add_memory(service, "mem-other", "evt-other", scope=OTHER_SCOPE)
    service.govern(
        _command(
            GovernanceAction.SEAL,
            EntityType.MEMORY,
            "mem-local",
            seconds=2,
        )
    )
    service.govern(
        _command(
            GovernanceAction.RESTORE,
            EntityType.MEMORY,
            "mem-local",
            seconds=3,
        )
    )

    first = service.audit(SCOPE, limit=100)
    repeated = service.audit(SCOPE, limit=100)

    assert repeated == first
    assert tuple(record.action for record in first[:2]) == ("restore", "seal")
    assert {record.action for record in first[2:]} == {"ingest", "materialize"}
    assert tuple(record.occurred_at for record in first) == tuple(
        sorted((record.occurred_at for record in first), reverse=True)
    )
    assert all(record.scope == SCOPE for record in first)
    assert service.audit(SCOPE, limit=1) == (first[0],)
    for invalid_limit in (0, 101, True):
        with pytest.raises(ValidationError, match="limit"):
            service.audit(SCOPE, limit=invalid_limit)
    with pytest.raises(ValidationError, match="scope"):
        service.audit("tenant", limit=1)  # type: ignore[arg-type]


def test_audit_and_tombstones_are_body_free_for_every_lifecycle_action(
    tmp_path: Path,
) -> None:
    path = tmp_path / "body-free.db"
    service = _service(path)
    receipts = []

    _add_memory(service, "mem-forget", "evt-forget")
    receipts.append(service.ingest_event(_event("evt-forget")))
    service.govern(
        _command(
            GovernanceAction.SEAL,
            EntityType.MEMORY,
            "mem-forget",
            seconds=1,
        )
    )
    service.govern(
        _command(
            GovernanceAction.RESTORE,
            EntityType.MEMORY,
            "mem-forget",
            seconds=2,
        )
    )
    service.govern(
        _command(
            GovernanceAction.FORGET,
            EntityType.MEMORY,
            "mem-forget",
            seconds=3,
        )
    )

    _add_memory(service, "mem-delete", "evt-memory-delete")
    receipts.append(service.ingest_event(_event("evt-memory-delete")))
    service.govern(
        _command(
            GovernanceAction.DELETE,
            EntityType.MEMORY,
            "mem-delete",
            seconds=4,
        )
    )

    _add_memory(service, "mem-cascade", "evt-raw-delete")
    receipts.append(service.ingest_event(_event("evt-raw-delete")))
    service.govern(
        _command(
            GovernanceAction.DELETE,
            EntityType.RAW_EVENT,
            "evt-raw-delete",
            seconds=5,
        )
    )

    expiring = _event(
        "evt-expire",
        occurred_at=BASE - timedelta(hours=2),
        raw_ttl=timedelta(hours=1),
    )
    receipts.append(service.ingest_event(expiring))
    assert service.expire_raw_content(as_of=BASE) == 1

    public_records = service.audit(SCOPE)
    assert {record.action for record in public_records} >= {
        "ingest",
        "materialize",
        "expire",
        "seal",
        "restore",
        "forget",
        "delete",
    }
    assert BODY_SENTINEL not in repr(public_records)

    content_hashes = {receipt.content_hash for receipt in receipts}
    with sqlite3.connect(path) as connection:
        audit_columns = [
            row[1] for row in connection.execute("PRAGMA table_info(audit_log)")
        ]
        audit_rows = connection.execute("SELECT * FROM audit_log").fetchall()
        tombstone_columns = [
            row[1] for row in connection.execute("PRAGMA table_info(tombstones)")
        ]
        tombstone_rows = connection.execute("SELECT * FROM tombstones").fetchall()

    persisted_audit = repr((audit_columns, audit_rows))
    persisted_tombstones = repr((tombstone_columns, tombstone_rows))
    assert BODY_SENTINEL not in persisted_audit
    assert BODY_SENTINEL not in persisted_tombstones
    assert all(content_hash not in persisted_audit for content_hash in content_hashes)
    assert all(
        content_hash not in persisted_tombstones for content_hash in content_hashes
    )
    assert "content" not in audit_columns
    assert "content" not in tombstone_columns


def test_caller_reason_matching_body_is_never_persisted_or_public(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reason-body-free.db"
    service = _service(path)
    _add_memory(service, "mem-reason", "evt-reason")

    command = GovernanceCommand(
        scope=SCOPE,
        action=GovernanceAction.FORGET,
        entity_type=EntityType.MEMORY,
        entity_id="mem-reason",
        occurred_at=BASE + timedelta(seconds=1),
        reason=BODY_SENTINEL,
    )
    result = service.govern(command)

    assert result.command is command
    records = service.audit(SCOPE)
    assert BODY_SENTINEL not in repr(records)
    governance_record = next(record for record in records if record.action == "forget")
    assert governance_record.reason == "governance requested"
    with sqlite3.connect(path) as connection:
        audit_columns = connection.execute("PRAGMA table_info(audit_log)").fetchall()
        audit_rows = connection.execute("SELECT * FROM audit_log").fetchall()
        tombstone_columns = connection.execute(
            "PRAGMA table_info(tombstones)"
        ).fetchall()
        tombstone_rows = connection.execute("SELECT * FROM tombstones").fetchall()
    assert BODY_SENTINEL not in repr((audit_columns, audit_rows))
    assert BODY_SENTINEL not in repr((tombstone_columns, tombstone_rows))


@pytest.mark.parametrize("operation", ["expire", "govern", "audit"])
@pytest.mark.parametrize(
    "ledger",
    ["missing", "empty", "zero", "zero-and-one", "text-one"],
)
def test_every_repository_transaction_requires_exact_schema_v1(
    tmp_path: Path,
    operation: str,
    ledger: str,
) -> None:
    path = tmp_path / f"schema-{operation}-{ledger}.db"
    service = _service(path)
    service.ingest_event(
        _event(
            "evt-schema",
            occurred_at=BASE - timedelta(hours=2),
            raw_ttl=timedelta(hours=1),
        )
    )
    with sqlite3.connect(path) as connection:
        if ledger == "missing":
            connection.execute("DROP TABLE schema_migrations")
        elif ledger == "empty":
            connection.execute("DELETE FROM schema_migrations")
        elif ledger == "zero":
            connection.execute("UPDATE schema_migrations SET version = 0")
        elif ledger == "zero-and-one":
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (0, "2026-08-13T09:00:00.000000+00:00"),
            )
        else:
            connection.execute("ALTER TABLE schema_migrations RENAME TO old_migrations")
            connection.execute(
                "CREATE TABLE schema_migrations (version TEXT, applied_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                ("1", "2026-08-13T09:00:00.000000+00:00"),
            )
            connection.execute("DROP TABLE old_migrations")
        before = {
            table: connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            ).fetchall()
            for table in (
                "raw_events",
                "raw_event_content",
                "memories",
                "memory_sources",
                "audit_log",
                "tombstones",
            )
        }

    with pytest.raises(RepositoryCorruption) as error:
        if operation == "expire":
            service.expire_raw_content(as_of=BASE)
        elif operation == "govern":
            service.govern(
                _command(
                    GovernanceAction.DELETE,
                    EntityType.RAW_EVENT,
                    "evt-schema",
                    seconds=1,
                )
            )
        else:
            service.audit(SCOPE)

    assert error.value.__cause__ is None
    with sqlite3.connect(path) as connection:
        after = {
            table: connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            ).fetchall()
            for table in before
        }
    assert after == before


def test_raw_delete_tombstone_failure_rolls_back_every_table(tmp_path: Path) -> None:
    path = tmp_path / "rollback.db"
    service = _service(path)
    _add_memory(service, "mem-cascade", "evt-cascade")

    table_names = (
        "raw_events",
        "raw_event_content",
        "memories",
        "memory_sources",
        "audit_log",
        "tombstones",
    )
    with sqlite3.connect(path) as connection:
        before = {
            table_name: connection.execute(
                f"SELECT * FROM {table_name} ORDER BY rowid"
            ).fetchall()
            for table_name in table_names
        }
        connection.execute(
            "CREATE TRIGGER reject_cascade_tombstone BEFORE INSERT ON tombstones "
            "WHEN NEW.entity_type = 'memory' AND NEW.entity_id = 'mem-cascade' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic tombstone failure'); END"
        )

    with pytest.raises(RepositoryCorruption) as error:
        service.govern(
            _command(
                GovernanceAction.DELETE,
                EntityType.RAW_EVENT,
                "evt-cascade",
                seconds=1,
            )
        )

    assert "synthetic tombstone failure" not in str(error.value)
    assert BODY_SENTINEL not in str(error.value)
    assert error.value.__cause__ is None
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER reject_cascade_tombstone")
        after = {
            table_name: connection.execute(
                f"SELECT * FROM {table_name} ORDER BY rowid"
            ).fetchall()
            for table_name in table_names
        }
    assert after == before


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("occurred_at", "private-audit-time-sentinel"),
        ("metadata_json", "{private-audit-json-sentinel"),
        ("entity_type", "private-audit-type-sentinel"),
        ("action", "private-audit-action-sentinel"),
        ("reason", "private-audit-reason-sentinel"),
        ("metadata_json", '{"unexpected":true}'),
    ],
)
def test_audit_rejects_corrupt_rows_without_disclosure(
    tmp_path: Path,
    column: str,
    value: str,
) -> None:
    path = tmp_path / f"audit-corrupt-{column}.db"
    service = _service(path)
    service.ingest_event(_event("evt-corrupt"))
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(f"UPDATE audit_log SET {column} = ?", (value,))

    with pytest.raises(RepositoryCorruption) as error:
        service.audit(SCOPE)

    assert value not in str(error.value)
    assert BODY_SENTINEL not in str(error.value)
    assert error.value.__cause__ is None


def test_audit_rejects_offset_overflow_timestamp_without_disclosure(
    tmp_path: Path,
) -> None:
    path = tmp_path / "audit-offset-overflow.db"
    service = _service(path)
    service.ingest_event(_event("evt-offset-overflow"))
    stored_value = "9999-12-31T23:59:59.999999-23:59"
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE audit_log SET occurred_at = ?", (stored_value,))

    with pytest.raises(RepositoryCorruption) as error:
        service.audit(SCOPE)

    assert stored_value not in str(error.value)
    assert BODY_SENTINEL not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize(
    ("action", "entity_type"),
    [
        ("ingest", "memory"),
        ("materialize", "raw_event"),
        ("expire", "memory"),
        ("seal", "raw_event"),
        ("restore", "raw_event"),
        ("forget", "raw_event"),
    ],
)
def test_audit_rejects_impossible_action_entity_pairs(
    tmp_path: Path,
    action: str,
    entity_type: str,
) -> None:
    path = tmp_path / f"audit-pair-{action}.db"
    service = _service(path)
    service.ingest_event(_event("evt-pair"))
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE audit_log SET action = ?, entity_type = ?",
            (action, entity_type),
        )

    with pytest.raises(RepositoryCorruption) as error:
        service.audit(SCOPE)

    assert error.value.__cause__ is None


def test_audit_rechecks_schema_inside_read_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "audit-version.db"
    repository = SQLiteRepository(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (2, "2026-08-13T10:00:00.000000+00:00"),
        )

    with pytest.raises(RepositoryCorruption, match="schema version"):
        TrueHeart(repository).audit(SCOPE)


def test_recall_rechecks_schema_inside_read_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "recall-version.db"
    repository = SQLiteRepository(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (2, "2026-08-13T10:00:00.000000+00:00"),
        )

    with pytest.raises(RepositoryCorruption, match="schema version"):
        TrueHeart(repository).recall(RecallQuery(scope=SCOPE, as_of=BASE))
