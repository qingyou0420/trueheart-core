import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from multiprocessing import get_context
from pathlib import Path
from queue import Empty
from threading import Barrier
from typing import Protocol

import pytest

from trueheart_core import (
    EntityDeleted,
    IdempotencyConflict,
    RawEventDraft,
    RepositoryCorruption,
    RetentionPolicy,
    Scope,
    SourceRef,
    SQLiteRepository,
    TrueHeart,
    TrustLevel,
    ValidationError,
)


class _ProcessStartEvent(Protocol):
    def wait(self, timeout: float | None = None) -> bool: ...


class _ProcessResultQueue(Protocol):
    def put(self, item: tuple[str, str]) -> None: ...


def _initialize_repository_process(
    path: str,
    start_event: _ProcessStartEvent,
    results: _ProcessResultQueue,
) -> None:
    results.put(("ready", ""))
    if not start_event.wait(timeout=10.0):
        results.put(("error", "start timeout"))
        return
    try:
        SQLiteRepository(path)
    except Exception as error:  # noqa: BLE001 - child reports failures to parent
        results.put(("error", type(error).__name__))
    else:
        results.put(("ok", ""))


def _draft(
    *,
    event_id: str = "evt-1",
    scope: Scope | None = None,
    content: str = "synthetic message",
    source_id: str = "message-1",
    source_type: str = "conversation",
    occurred_at: datetime | None = None,
    trust: TrustLevel = TrustLevel.OBSERVED,
    source_metadata: dict[str, object] | None = None,
    raw_ttl: timedelta = timedelta(days=7),
    clear_for: timedelta = timedelta(days=7),
    recall_for: timedelta = timedelta(days=30),
    event_metadata: dict[str, object] | None = None,
) -> RawEventDraft:
    return RawEventDraft(
        event_id=event_id,
        scope=scope or Scope("tenant", "owner", "subject"),
        source=SourceRef(
            source_id=source_id,
            source_type=source_type,
            occurred_at=occurred_at or datetime(2026, 8, 13, 9, tzinfo=UTC),
            trust=trust,
            metadata=source_metadata or {"channel": "chat"},  # type: ignore[arg-type]
        ),
        content=content,
        retention=RetentionPolicy(
            raw_ttl=raw_ttl,
            clear_for=clear_for,
            recall_for=recall_for,
        ),
        metadata=event_metadata or {"labels": ["example"]},  # type: ignore[arg-type]
    )


def _service(path: Path) -> TrueHeart:
    return TrueHeart(
        SQLiteRepository(path),
        clock=lambda: datetime(2026, 8, 13, 10, tzinfo=UTC),
    )


def test_ingest_returns_body_free_receipt_and_persists_across_instances(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trueheart.db"

    receipt = _service(path).ingest_event(_draft())
    persisted = _service(path).ingest_event(_draft())

    assert receipt == persisted
    assert receipt.event_id == "evt-1"
    assert receipt.content_hash == (
        "5f241eec5564d5a9b1aa6adf128bdbbdb5529ad99af91e7cd2ded2107e5ea3e2"
    )
    assert receipt.ingested_at == datetime(2026, 8, 13, 10, tzinfo=UTC)
    assert receipt.raw_expires_at == datetime(2026, 8, 20, 9, tzinfo=UTC)
    assert receipt.content_available is True
    assert not hasattr(receipt, "content")


def test_identical_ingest_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "trueheart.db"
    service = _service(path)

    first = service.ingest_event(_draft())
    second = service.ingest_event(_draft())

    assert second == first
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM raw_events").fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM raw_event_content"
        ).fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM audit_log").fetchone() == (1,)


def test_same_event_id_with_changed_body_raises_conflict(tmp_path: Path) -> None:
    path = tmp_path / "trueheart.db"
    service = _service(path)
    service.ingest_event(_draft())

    with pytest.raises(IdempotencyConflict) as error:
        service.ingest_event(_draft(content="changed synthetic message"))

    assert "changed synthetic message" not in str(error.value)


def test_same_event_id_in_another_scope_is_independent(tmp_path: Path) -> None:
    path = tmp_path / "trueheart.db"
    service = _service(path)
    first = service.ingest_event(_draft())
    other_scope = Scope("tenant", "owner", "other-subject")

    second = service.ingest_event(_draft(scope=other_scope))

    assert first.scope == Scope("tenant", "owner", "subject")
    assert second.scope == other_scope
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM raw_events").fetchone() == (2,)


def test_tombstoned_event_id_cannot_be_reused(tmp_path: Path) -> None:
    path = tmp_path / "trueheart.db"
    service = _service(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO tombstones ("
            "tenant_id, owner_id, subject_id, entity_type, entity_id, deleted_at, "
            "reason, dependency_fingerprint, metadata_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "tenant",
                "owner",
                "subject",
                "raw_event",
                "evt-1",
                "2026-08-13T09:30:00.000000+00:00",
                "governance requested",
                None,
                "{}",
            ),
        )

    with pytest.raises(EntityDeleted) as error:
        service.ingest_event(_draft())

    assert "synthetic message" not in str(error.value)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM raw_events").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM audit_log").fetchone() == (0,)


def test_ingest_replay_rejects_active_receipt_without_body(tmp_path: Path) -> None:
    path = tmp_path / "missing-active-body.db"
    service = _service(path)
    service.ingest_event(_draft())
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM raw_event_content WHERE event_id = ?", ("evt-1",)
        )

    with pytest.raises(RepositoryCorruption) as error:
        service.ingest_event(_draft())

    assert "synthetic message" not in str(error.value)
    assert error.value.__cause__ is None


def test_schema_version_newer_than_supported_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (2, "2026-08-13T10:00:00.000000+00:00"),
        )

    with pytest.raises(RepositoryCorruption, match="schema version"):
        SQLiteRepository(path)


def test_memory_database_path_is_rejected() -> None:
    with pytest.raises(ValidationError, match="path"):
        SQLiteRepository(":memory:")


def test_failed_schema_initialization_rolls_back_all_trueheart_tables(
    tmp_path: Path,
) -> None:
    path = tmp_path / "failed-init.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TRIGGER reject_schema_version BEFORE INSERT "
            "ON schema_migrations BEGIN "
            "SELECT RAISE(ABORT, 'synthetic-init-sentinel'); END"
        )

    with pytest.raises(RepositoryCorruption) as error:
        SQLiteRepository(path)

    assert "synthetic-init-sentinel" not in str(error.value)
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = ?",
                ("table",),
            )
        }
    assert tables == {"schema_migrations"}


def test_concurrent_repository_initializers_are_consistent(tmp_path: Path) -> None:
    path = tmp_path / "concurrent-init.db"
    barrier = Barrier(4)

    def initialize() -> None:
        barrier.wait()
        SQLiteRepository(path)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(initialize) for _ in range(4)]
        for future in futures:
            future.result()

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall() == [(1,)]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = ?",
                ("table",),
            )
        }
    assert tables == {
        "audit_log",
        "memories",
        "memory_sources",
        "raw_event_content",
        "raw_events",
        "schema_migrations",
        "tombstones",
    }


def test_concurrent_process_repository_initializers_are_consistent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "concurrent-process-init.db"
    context = get_context("spawn")
    start_event = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_initialize_repository_process,
            args=(str(path), start_event, results),
        )
        for _ in range(24)
    ]
    try:
        for process in processes:
            process.start()
        ready = [results.get(timeout=20.0) for _ in processes]
        assert ready == [("ready", "")] * len(processes)

        start_event.set()
        outcomes = [results.get(timeout=30.0) for _ in processes]
        for process in processes:
            process.join(timeout=10.0)
        assert all(not process.is_alive() for process in processes)
        assert all(process.exitcode == 0 for process in processes)
        assert outcomes == [("ok", "")] * len(processes)
    except Empty:
        pytest.fail("constructor process did not report within the bounded timeout")
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5.0)
        results.close()
        results.join_thread()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall() == [(1,)]


def test_ingest_rechecks_schema_version_inside_write_transaction(
    tmp_path: Path,
) -> None:
    path = tmp_path / "version-race.db"
    repository = SQLiteRepository(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (2, "2026-08-13T10:00:00.000000+00:00"),
        )

    service = TrueHeart(
        repository,
        clock=lambda: datetime(2026, 8, 13, 10, tzinfo=UTC),
    )
    with pytest.raises(RepositoryCorruption, match="schema version"):
        service.ingest_event(_draft())

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM raw_events").fetchone() == (0,)


def test_ingest_translates_sqlite_failure_without_body_or_diagnostic(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sqlite-failure.db"
    service = _service(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_raw_event BEFORE INSERT ON raw_events BEGIN "
            "SELECT RAISE(ABORT, "
            "'synthetic-sql-sentinel synthetic message'); END"
        )

    with pytest.raises(RepositoryCorruption) as error:
        service.ingest_event(_draft())

    assert "synthetic-sql-sentinel" not in str(error.value)
    assert "synthetic message" not in str(error.value)
    assert error.value.__cause__ is None
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM raw_events").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM audit_log").fetchone() == (0,)


@pytest.mark.parametrize(
    ("statement", "value"),
    [
        (
            (
                "UPDATE raw_events SET source_occurred_at = ? WHERE tenant_id = ? "
                "AND owner_id = ? AND subject_id = ? AND event_id = ?"
            ),
            "synthetic-corruption-sentinel",
        ),
        (
            (
                "UPDATE raw_events SET source_trust = ? WHERE tenant_id = ? "
                "AND owner_id = ? AND subject_id = ? AND event_id = ?"
            ),
            9,
        ),
        (
            (
                "UPDATE raw_events SET clear_for_microseconds = ? "
                "WHERE tenant_id = ? AND owner_id = ? AND subject_id = ? "
                "AND event_id = ?"
            ),
            "synthetic-corruption-sentinel",
        ),
        (
            (
                "UPDATE raw_events SET source_metadata_json = ? WHERE tenant_id = ? "
                "AND owner_id = ? AND subject_id = ? AND event_id = ?"
            ),
            "{synthetic-corruption-sentinel",
        ),
        (
            (
                "UPDATE raw_events SET source_id = ? WHERE tenant_id = ? "
                "AND owner_id = ? AND subject_id = ? AND event_id = ?"
            ),
            sqlite3.Binary(b"synthetic-corruption-sentinel"),
        ),
        (
            (
                "UPDATE raw_events SET source_metadata_json = ? WHERE tenant_id = ? "
                "AND owner_id = ? AND subject_id = ? AND event_id = ?"
            ),
            '{"channel": "chat"}',
        ),
    ],
    ids=[
        "date",
        "trust",
        "numeric",
        "json",
        "sqlite-type",
        "noncanonical-json",
    ],
)
def test_corrupt_persisted_event_raises_body_free_repository_corruption(
    tmp_path: Path,
    statement: str,
    value: object,
) -> None:
    path = tmp_path / "corrupt-row.db"
    service = _service(path)
    service.ingest_event(_draft())
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            statement,
            (value, "tenant", "owner", "subject", "evt-1"),
        )

    with pytest.raises(RepositoryCorruption) as error:
        service.ingest_event(_draft())

    assert "synthetic-corruption-sentinel" not in str(error.value)
    assert "synthetic message" not in str(error.value)
    assert error.value.__cause__ is None


def _changed_canonical_draft(field: str) -> RawEventDraft:
    if field == "source_id":
        return _draft(source_id="message-2")
    if field == "source_type":
        return _draft(source_type="import")
    if field == "occurred_at":
        return _draft(occurred_at=datetime(2026, 8, 13, 9, 1, tzinfo=UTC))
    if field == "trust":
        return _draft(trust=TrustLevel.CONFIRMED)
    if field == "source_metadata":
        return _draft(source_metadata={"channel": "import"})
    if field == "raw_ttl":
        return _draft(raw_ttl=timedelta(days=8))
    if field == "clear_for":
        return _draft(clear_for=timedelta(days=8))
    if field == "recall_for":
        return _draft(recall_for=timedelta(days=31))
    if field == "event_metadata":
        return _draft(event_metadata={"labels": ["changed"]})
    if field == "body":
        return _draft(content="changed synthetic message")
    raise AssertionError(f"unknown test field: {field}")


@pytest.mark.parametrize(
    "field",
    [
        "source_id",
        "source_type",
        "occurred_at",
        "trust",
        "source_metadata",
        "raw_ttl",
        "clear_for",
        "recall_for",
        "event_metadata",
        "body",
    ],
)
def test_each_changed_canonical_ingest_field_conflicts(
    tmp_path: Path,
    field: str,
) -> None:
    path = tmp_path / "canonical-conflict.db"
    service = _service(path)
    service.ingest_event(_draft())

    with pytest.raises(IdempotencyConflict):
        service.ingest_event(_changed_canonical_draft(field))

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_log").fetchone() == (1,)
