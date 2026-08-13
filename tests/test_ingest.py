import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

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


def _draft(
    *,
    event_id: str = "evt-1",
    scope: Scope | None = None,
    content: str = "synthetic message",
) -> RawEventDraft:
    return RawEventDraft(
        event_id=event_id,
        scope=scope or Scope("tenant", "owner", "subject"),
        source=SourceRef(
            source_id="message-1",
            source_type="conversation",
            occurred_at=datetime(2026, 8, 13, 9, tzinfo=UTC),
            trust=TrustLevel.OBSERVED,
            metadata={"channel": "chat"},
        ),
        content=content,
        retention=RetentionPolicy(
            raw_ttl=timedelta(days=7),
            clear_for=timedelta(days=7),
            recall_for=timedelta(days=30),
        ),
        metadata={"labels": ["example"]},
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
                "synthetic deletion",
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
