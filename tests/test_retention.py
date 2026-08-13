import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trueheart_core import (
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
OCCURRED_AT = datetime(2026, 8, 13, 9, tzinfo=UTC)
EXPIRES_AT = datetime(2026, 8, 13, 11, tzinfo=UTC)


def _event() -> RawEventDraft:
    return RawEventDraft(
        event_id="evt-retention",
        scope=SCOPE,
        source=SourceRef(
            source_id="source-retention",
            source_type="synthetic",
            occurred_at=OCCURRED_AT,
            trust=TrustLevel.OBSERVED,
        ),
        content="private synthetic raw retention body",
        retention=RetentionPolicy(
            raw_ttl=timedelta(hours=2),
            clear_for=timedelta(days=2),
            recall_for=timedelta(days=6),
        ),
    )


def _service(path: Path) -> TrueHeart:
    return TrueHeart(
        SQLiteRepository(path),
        clock=lambda: datetime(2026, 8, 13, 10, tzinfo=UTC),
    )


def test_expiry_boundary_preserves_receipt_lineage_and_derived_memory(
    tmp_path: Path,
) -> None:
    path = tmp_path / "retention.db"
    service = _service(path)
    original_receipt = service.ingest_event(_event())
    original_memory = service.materialize_once(
        MemoryDraft(
            memory_id="mem-retention",
            scope=SCOPE,
            content="synthetic retained derived memory",
            source_event_ids=("evt-retention",),
            kind="fact",
            trust=TrustLevel.OBSERVED,
            created_at=datetime(2026, 8, 13, 10, tzinfo=UTC),
        )
    )

    assert service.expire_raw_content(as_of=EXPIRES_AT - timedelta(microseconds=1)) == 0
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT content FROM raw_event_content WHERE event_id = ?",
            ("evt-retention",),
        ).fetchone() == ("private synthetic raw retention body",)

    assert service.expire_raw_content(as_of=EXPIRES_AT) == 1
    expired_receipt = service.ingest_event(_event())
    recalled = service.recall(RecallQuery(scope=SCOPE, as_of=EXPIRES_AT))

    assert expired_receipt.content_available is False
    assert expired_receipt.content_hash == original_receipt.content_hash
    assert expired_receipt.source == original_receipt.source
    assert tuple(item.memory for item in recalled) == (original_memory,)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM raw_event_content"
        ).fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM raw_events").fetchone() == (1,)
        assert connection.execute(
            "SELECT status FROM raw_events WHERE event_id = ?",
            ("evt-retention",),
        ).fetchone() == ("expired",)
        assert connection.execute("SELECT COUNT(*) FROM memories").fetchone() == (1,)
        assert connection.execute(
            "SELECT event_id FROM memory_sources WHERE memory_id = ?",
            ("mem-retention",),
        ).fetchall() == [("evt-retention",)]
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = ?", ("expire",)
        ).fetchone() == (1,)


def test_repeated_expiry_is_idempotent_without_new_audit(tmp_path: Path) -> None:
    path = tmp_path / "repeat.db"
    service = _service(path)
    service.ingest_event(_event())

    assert service.expire_raw_content(as_of=EXPIRES_AT) == 1
    assert service.expire_raw_content(as_of=EXPIRES_AT + timedelta(days=1)) == 0

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = ?", ("expire",)
        ).fetchone() == (1,)


def test_expiry_rejects_naive_as_of_before_repository_access(tmp_path: Path) -> None:
    path = tmp_path / "naive.db"
    service = _service(path)

    with pytest.raises(ValidationError, match="as_of"):
        service.expire_raw_content(as_of=EXPIRES_AT.replace(tzinfo=None))

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_log").fetchone() == (0,)


def test_expiry_rechecks_schema_and_rejects_noncanonical_timestamp(
    tmp_path: Path,
) -> None:
    version_path = tmp_path / "expiry-version.db"
    repository = SQLiteRepository(version_path)
    with sqlite3.connect(version_path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (2, "2026-08-13T10:00:00.000000+00:00"),
        )
    with pytest.raises(RepositoryCorruption, match="schema version"):
        TrueHeart(repository).expire_raw_content(as_of=EXPIRES_AT)

    corrupt_path = tmp_path / "expiry-corrupt.db"
    service = _service(corrupt_path)
    service.ingest_event(_event())
    with sqlite3.connect(corrupt_path) as connection:
        connection.execute(
            "UPDATE raw_events SET raw_expires_at = ? WHERE event_id = ?",
            ("2026-08-13T19:00:00.000000+08:00", "evt-retention"),
        )

    with pytest.raises(RepositoryCorruption) as error:
        service.expire_raw_content(as_of=EXPIRES_AT)

    assert error.value.__cause__ is None
    with sqlite3.connect(corrupt_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM raw_event_content"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = ?", ("expire",)
        ).fetchone() == (0,)
