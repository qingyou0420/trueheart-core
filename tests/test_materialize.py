import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trueheart_core import (
    EntityDeleted,
    EntityNotFound,
    IdempotencyConflict,
    MemoryDraft,
    MemoryStatus,
    RawEventDraft,
    RepositoryCorruption,
    RetentionPolicy,
    Scope,
    ScopeMismatch,
    SourceRef,
    SQLiteRepository,
    TrueHeart,
    TrustEscalation,
    TrustLevel,
    ValidationError,
)

SCOPE = Scope("tenant", "owner", "subject")
CREATED_AT = datetime(2026, 8, 13, 12, tzinfo=UTC)


def _service(path: Path) -> TrueHeart:
    return TrueHeart(
        SQLiteRepository(path),
        clock=lambda: datetime(2026, 8, 13, 10, tzinfo=UTC),
    )


def _event(
    event_id: str,
    *,
    scope: Scope = SCOPE,
    trust: TrustLevel = TrustLevel.OBSERVED,
    clear_for: timedelta = timedelta(days=4),
    recall_for: timedelta = timedelta(days=10),
) -> RawEventDraft:
    return RawEventDraft(
        event_id=event_id,
        scope=scope,
        source=SourceRef(
            source_id=f"source-{event_id}",
            source_type="synthetic",
            occurred_at=datetime(2026, 8, 13, 9, tzinfo=UTC),
            trust=trust,
        ),
        content=f"synthetic body for {event_id}",
        retention=RetentionPolicy(
            raw_ttl=timedelta(hours=1),
            clear_for=clear_for,
            recall_for=recall_for,
        ),
    )


def _memory(
    *,
    memory_id: str = "mem-1",
    scope: Scope = SCOPE,
    content: str = "synthetic derived memory",
    source_event_ids: tuple[str, ...] = ("evt-1", "evt-2"),
    kind: str = "fact",
    trust: TrustLevel = TrustLevel.OBSERVED,
) -> MemoryDraft:
    return MemoryDraft(
        memory_id=memory_id,
        scope=scope,
        content=content,
        source_event_ids=source_event_ids,
        kind=kind,
        trust=trust,
        created_at=CREATED_AT,
        metadata={"label": "synthetic"},
    )


def _ingest_sources(service: TrueHeart) -> None:
    service.ingest_event(
        _event(
            "evt-1",
            clear_for=timedelta(days=4),
            recall_for=timedelta(days=6),
        )
    )
    service.ingest_event(
        _event(
            "evt-2",
            trust=TrustLevel.CONFIRMED,
            clear_for=timedelta(days=2),
            recall_for=timedelta(days=8),
        )
    )


def test_materialize_requires_at_least_one_source() -> None:
    with pytest.raises(ValidationError, match="source_event_ids"):
        _memory(source_event_ids=())


def test_materialize_rejects_missing_or_cross_scope_sources(tmp_path: Path) -> None:
    service = _service(tmp_path / "sources.db")
    service.ingest_event(_event("evt-1"))
    other_scope = Scope("tenant", "owner", "other-subject")
    service.ingest_event(_event("evt-other", scope=other_scope))

    with pytest.raises(EntityNotFound, match="evt-missing") as missing:
        service.materialize_once(_memory(source_event_ids=("evt-1", "evt-missing")))
    with pytest.raises(ScopeMismatch, match="evt-other") as mismatch:
        service.materialize_once(_memory(source_event_ids=("evt-1", "evt-other")))

    assert "synthetic body" not in str(missing.value)
    assert "synthetic body" not in str(mismatch.value)
    with sqlite3.connect(tmp_path / "sources.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM memories").fetchone() == (0,)


def test_materialize_cannot_exceed_least_trusted_source(tmp_path: Path) -> None:
    service = _service(tmp_path / "trust.db")
    service.ingest_event(_event("evt-1", trust=TrustLevel.UNTRUSTED))
    service.ingest_event(_event("evt-2", trust=TrustLevel.CONFIRMED))

    with pytest.raises(TrustEscalation, match="mem-1"):
        service.materialize_once(_memory(trust=TrustLevel.OBSERVED))

    accepted = service.materialize_once(_memory(trust=TrustLevel.UNTRUSTED))
    assert accepted.trust is TrustLevel.UNTRUSTED


def test_materialize_writes_memory_and_all_edges_atomically(tmp_path: Path) -> None:
    path = tmp_path / "atomic.db"
    service = _service(path)
    _ingest_sources(service)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM raw_event_content WHERE tenant_id = ? AND owner_id = ? "
            "AND subject_id = ? AND event_id = ?",
            ("tenant", "owner", "subject", "evt-1"),
        )

    record = service.materialize_once(_memory())

    canonical = (
        '{"kind":"fact","scope":{"owner_id":"owner",'
        '"subject_id":"subject","tenant_id":"tenant"},'
        '"source_event_ids":["evt-1","evt-2"]}'
    )
    assert (
        record.dependency_fingerprint
        == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    )
    assert record.source_event_ids == ("evt-1", "evt-2")
    assert record.clear_until == datetime(2026, 8, 15, 12, tzinfo=UTC)
    assert record.recall_until == datetime(2026, 8, 19, 12, tzinfo=UTC)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM memories").fetchone() == (1,)
        assert connection.execute(
            "SELECT event_id FROM memory_sources ORDER BY event_id"
        ).fetchall() == [("evt-1",), ("evt-2",)]
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = ?", ("materialize",)
        ).fetchone() == (1,)

    rollback_path = tmp_path / "rollback.db"
    rollback_service = _service(rollback_path)
    _ingest_sources(rollback_service)
    with sqlite3.connect(rollback_path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_second_edge BEFORE INSERT ON memory_sources "
            "WHEN NEW.event_id = 'evt-2' BEGIN SELECT RAISE(ABORT, 'sentinel'); END"
        )
    with pytest.raises(RepositoryCorruption) as error:
        rollback_service.materialize_once(_memory())
    assert "sentinel" not in str(error.value)
    assert "synthetic body" not in str(error.value)
    assert error.value.__cause__ is None
    with sqlite3.connect(rollback_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM memories").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM memory_sources").fetchone() == (
            0,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = ?", ("materialize",)
        ).fetchone() == (0,)


def test_materialize_translates_corrupt_source_without_body(tmp_path: Path) -> None:
    path = tmp_path / "corrupt-source.db"
    service = _service(path)
    service.ingest_event(_event("evt-1"))
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE raw_events SET source_trust = ? WHERE tenant_id = ? "
            "AND owner_id = ? AND subject_id = ? AND event_id = ?",
            (9, "tenant", "owner", "subject", "evt-1"),
        )

    with pytest.raises(RepositoryCorruption) as error:
        service.materialize_once(_memory(source_event_ids=("evt-1",)))

    assert "synthetic body" not in str(error.value)
    assert error.value.__cause__ is None


def test_identical_materialization_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "idempotent.db"
    service = _service(path)
    _ingest_sources(service)

    first = service.materialize_once(_memory(source_event_ids=("evt-2", "evt-1")))
    second = service.materialize_once(_memory(source_event_ids=("evt-2", "evt-1")))

    assert second == first
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM memories").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM memory_sources").fetchone() == (
            2,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = ?", ("materialize",)
        ).fetchone() == (1,)


def test_identical_materialization_preserves_sealed_lifecycle_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sealed-idempotent.db"
    service = _service(path)
    _ingest_sources(service)
    service.materialize_once(_memory())
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE memories SET status = ? WHERE memory_id = ?",
            (MemoryStatus.SEALED.value, "mem-1"),
        )

    repeated = service.materialize_once(_memory())

    assert repeated.status is MemoryStatus.SEALED
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = ?", ("materialize",)
        ).fetchone() == (1,)


def test_changed_memory_with_same_id_conflicts(tmp_path: Path) -> None:
    path = tmp_path / "conflict.db"
    service = _service(path)
    _ingest_sources(service)
    service.materialize_once(_memory())

    with pytest.raises(IdempotencyConflict) as error:
        service.materialize_once(_memory(content="changed synthetic derived memory"))

    assert "changed synthetic derived memory" not in str(error.value)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = ?", ("materialize",)
        ).fetchone() == (1,)


def test_tombstoned_id_or_dependency_fingerprint_cannot_rematerialize(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tombstones.db"
    service = _service(path)
    _ingest_sources(service)
    canonical = (
        '{"kind":"fact","scope":{"owner_id":"owner",'
        '"subject_id":"subject","tenant_id":"tenant"},'
        '"source_event_ids":["evt-1","evt-2"]}'
    )
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO tombstones (tenant_id, owner_id, subject_id, entity_type, "
            "entity_id, deleted_at, reason, dependency_fingerprint, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "tenant",
                    "owner",
                    "subject",
                    "memory",
                    "mem-blocked",
                    "2026-08-13T11:00:00.000000+00:00",
                    "synthetic deletion",
                    None,
                    "{}",
                ),
                (
                    "tenant",
                    "owner",
                    "subject",
                    "memory",
                    "old-memory-id",
                    "2026-08-13T11:00:00.000000+00:00",
                    "synthetic deletion",
                    fingerprint,
                    "{}",
                ),
            ],
        )

    with pytest.raises(EntityDeleted, match="mem-blocked"):
        service.materialize_once(_memory(memory_id="mem-blocked", kind="other"))
    with pytest.raises(EntityDeleted, match="mem-new"):
        service.materialize_once(_memory(memory_id="mem-new"))

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM memories").fetchone() == (0,)
