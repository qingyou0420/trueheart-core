import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trueheart_core import (
    EntityDeleted,
    EntityNotFound,
    EntityType,
    GovernanceAction,
    GovernanceCommand,
    InvalidTransition,
    MemoryDraft,
    RawEventDraft,
    RecallQuery,
    RepositoryCorruption,
    RetentionPolicy,
    Scope,
    ScopeMismatch,
    SourceRef,
    SQLiteRepository,
    TrueHeart,
    TrustLevel,
    ValidationError,
)

SCOPE = Scope("tenant", "owner", "subject")
OTHER_SCOPE = Scope("tenant", "owner", "other-subject")
NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)


def _service(path: Path) -> TrueHeart:
    return TrueHeart(
        SQLiteRepository(path),
        clock=lambda: datetime(2026, 8, 13, 10, tzinfo=UTC),
    )


def _event(event_id: str, *, scope: Scope = SCOPE) -> RawEventDraft:
    return RawEventDraft(
        event_id=event_id,
        scope=scope,
        source=SourceRef(
            source_id=f"source-{event_id}",
            source_type="synthetic",
            occurred_at=datetime(2026, 8, 13, 9, tzinfo=UTC),
            trust=TrustLevel.OBSERVED,
        ),
        content=f"private synthetic event body {event_id}",
        retention=RetentionPolicy(
            raw_ttl=timedelta(days=1),
            clear_for=timedelta(days=2),
            recall_for=timedelta(days=6),
        ),
    )


def _memory(
    memory_id: str,
    source_event_ids: tuple[str, ...],
    *,
    scope: Scope = SCOPE,
    kind: str = "fact",
) -> MemoryDraft:
    return MemoryDraft(
        memory_id=memory_id,
        scope=scope,
        content=f"private synthetic memory body {memory_id}",
        source_event_ids=source_event_ids,
        kind=kind,
        trust=TrustLevel.OBSERVED,
        created_at=NOW,
    )


def _command(
    action: GovernanceAction,
    entity_type: EntityType,
    entity_id: str,
    *,
    scope: Scope = SCOPE,
    occurred_at: datetime = NOW,
    reason: str = "safe lifecycle reason",
) -> GovernanceCommand:
    return GovernanceCommand(
        scope=scope,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        occurred_at=occurred_at,
        reason=reason,
    )


def _add_memory(
    service: TrueHeart,
    memory_id: str = "mem-1",
    event_ids: tuple[str, ...] = ("evt-1",),
    *,
    scope: Scope = SCOPE,
    kind: str = "fact",
) -> None:
    for event_id in event_ids:
        service.ingest_event(_event(event_id, scope=scope))
    service.materialize_once(_memory(memory_id, event_ids, scope=scope, kind=kind))


def test_seal_excludes_active_memory_and_restore_recalls_it(tmp_path: Path) -> None:
    service = _service(tmp_path / "seal-restore.db")
    _add_memory(service)

    sealed = service.govern(_command(GovernanceAction.SEAL, EntityType.MEMORY, "mem-1"))
    while_sealed = service.recall(RecallQuery(scope=SCOPE, as_of=NOW))
    restored = service.govern(
        _command(
            GovernanceAction.RESTORE,
            EntityType.MEMORY,
            "mem-1",
            occurred_at=NOW + timedelta(seconds=1),
        )
    )
    after_restore = service.recall(RecallQuery(scope=SCOPE, as_of=NOW))

    assert sealed.affected_ids == ("mem-1",)
    assert while_sealed == ()
    assert restored.affected_ids == ("mem-1",)
    assert tuple(item.memory.memory_id for item in after_restore) == ("mem-1",)


def test_invalid_restore_states_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "invalid-restore.db"
    service = _service(path)
    _add_memory(service, "mem-active", ("evt-active",))
    _add_memory(service, "mem-forgotten", ("evt-forgotten",))
    _add_memory(service, "mem-deleted", ("evt-deleted",))

    with pytest.raises(InvalidTransition, match="mem-active"):
        service.govern(
            _command(GovernanceAction.RESTORE, EntityType.MEMORY, "mem-active")
        )
    service.govern(
        _command(GovernanceAction.FORGET, EntityType.MEMORY, "mem-forgotten")
    )
    service.govern(_command(GovernanceAction.DELETE, EntityType.MEMORY, "mem-deleted"))

    for memory_id in ("mem-forgotten", "mem-deleted"):
        with pytest.raises(EntityDeleted, match=memory_id):
            service.govern(
                _command(GovernanceAction.RESTORE, EntityType.MEMORY, memory_id)
            )


def test_forget_memory_removes_body_and_edges_and_blocks_resurrection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "forget.db"
    service = _service(path)
    _add_memory(service, "mem-forget", ("evt-a", "evt-b"))

    result = service.govern(
        _command(GovernanceAction.FORGET, EntityType.MEMORY, "mem-forget")
    )

    assert result.affected_ids == ("mem-forget",)
    with sqlite3.connect(path) as connection:
        tombstone = connection.execute(
            "SELECT entity_type, entity_id, dependency_fingerprint "
            "FROM tombstones WHERE entity_id = ?",
            ("mem-forget",),
        ).fetchone()
        assert tombstone is not None
        assert tombstone[0:2] == ("memory", "mem-forget")
        assert isinstance(tombstone[2], str) and len(tombstone[2]) == 64
        assert connection.execute("SELECT COUNT(*) FROM memories").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM memory_sources").fetchone() == (
            0,
        )
        assert connection.execute("SELECT COUNT(*) FROM raw_events").fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = ?", ("forget",)
        ).fetchone() == (1,)

    with pytest.raises(EntityDeleted, match="mem-forget"):
        service.materialize_once(_memory("mem-forget", ("evt-a", "evt-b")))
    with pytest.raises(EntityDeleted, match="mem-replacement"):
        service.materialize_once(_memory("mem-replacement", ("evt-b", "evt-a")))


def test_delete_memory_is_irreversible_and_audited_as_delete(tmp_path: Path) -> None:
    path = tmp_path / "delete-memory.db"
    service = _service(path)
    _add_memory(service, "mem-delete", ("evt-delete",))

    result = service.govern(
        _command(GovernanceAction.DELETE, EntityType.MEMORY, "mem-delete")
    )

    assert result.affected_ids == ("mem-delete",)
    assert service.recall(RecallQuery(scope=SCOPE, as_of=NOW)) == ()
    with pytest.raises(EntityDeleted, match="mem-delete"):
        service.materialize_once(_memory("mem-delete", ("evt-delete",)))
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = ?", ("delete",)
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM tombstones WHERE entity_type = ?",
            ("memory",),
        ).fetchone() == (1,)


def test_delete_raw_event_atomically_cascades_all_exact_scope_dependents(
    tmp_path: Path,
) -> None:
    path = tmp_path / "delete-raw.db"
    service = _service(path)
    for event_id in ("evt-target", "evt-shared", "evt-unrelated"):
        service.ingest_event(_event(event_id))
    memory_a = _memory("mem-a", ("evt-target",))
    memory_b = _memory("mem-b", ("evt-target", "evt-shared"), kind="plan")
    unrelated = _memory("mem-unrelated", ("evt-unrelated",))
    for draft in (memory_a, memory_b, unrelated):
        service.materialize_once(draft)

    result = service.govern(
        _command(GovernanceAction.DELETE, EntityType.RAW_EVENT, "evt-target")
    )

    assert result.affected_ids == ("evt-target", "mem-a", "mem-b")
    assert tuple(
        item.memory.memory_id
        for item in service.recall(RecallQuery(scope=SCOPE, as_of=NOW))
    ) == ("mem-unrelated",)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT event_id FROM raw_events ORDER BY event_id"
        ).fetchall() == [("evt-shared",), ("evt-unrelated",)]
        assert connection.execute(
            "SELECT memory_id FROM memories ORDER BY memory_id"
        ).fetchall() == [("mem-unrelated",)]
        assert connection.execute(
            "SELECT memory_id, event_id FROM memory_sources ORDER BY memory_id"
        ).fetchall() == [("mem-unrelated", "evt-unrelated")]
        assert connection.execute(
            "SELECT entity_type, entity_id FROM tombstones ORDER BY entity_type, entity_id"
        ).fetchall() == [
            ("memory", "mem-a"),
            ("memory", "mem-b"),
            ("raw_event", "evt-target"),
        ]
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = ? AND entity_id = ?",
            ("delete", "evt-target"),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = ? AND entity_id IN (?, ?)",
            ("delete", "mem-a", "mem-b"),
        ).fetchone() == (0,)

    with pytest.raises(EntityDeleted, match="evt-target"):
        service.ingest_event(_event("evt-target"))
    with pytest.raises(EntityDeleted, match="mem-a"):
        service.materialize_once(memory_a)
    with pytest.raises(EntityDeleted, match="mem-new"):
        service.materialize_once(
            _memory("mem-new", ("evt-shared", "evt-target"), kind="plan")
        )


def test_delete_raw_event_rejects_corrupt_dependent_fingerprint(
    tmp_path: Path,
) -> None:
    path = tmp_path / "delete-corrupt-lineage.db"
    service = _service(path)
    _add_memory(service, "mem-corrupt", ("evt-corrupt",))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE memories SET dependency_fingerprint = ? WHERE memory_id = ?",
            ("0" * 64, "mem-corrupt"),
        )

    with pytest.raises(RepositoryCorruption) as error:
        service.govern(
            _command(GovernanceAction.DELETE, EntityType.RAW_EVENT, "evt-corrupt")
        )

    assert error.value.__cause__ is None
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM raw_events").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM memories").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM memory_sources").fetchone() == (
            1,
        )
        assert connection.execute("SELECT COUNT(*) FROM tombstones").fetchone() == (0,)


def test_memory_governance_rejects_lineage_without_source_receipt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "govern-corrupt-lineage.db"
    service = _service(path)
    _add_memory(service, "mem-orphan", ("evt-orphan",))
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM raw_events WHERE event_id = ?", ("evt-orphan",))

    with pytest.raises(RepositoryCorruption) as error:
        service.govern(_command(GovernanceAction.SEAL, EntityType.MEMORY, "mem-orphan"))

    assert error.value.__cause__ is None
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT status FROM memories WHERE memory_id = ?", ("mem-orphan",)
        ).fetchone() == ("active",)
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = ?", ("seal",)
        ).fetchone() == (0,)


def test_governance_scope_diagnosis_prefers_exact_scope_without_body_projection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "scope.db"
    service = _service(path)
    _add_memory(service, "shared-id", ("shared-event",), scope=SCOPE)
    _add_memory(service, "shared-id", ("shared-event",), scope=OTHER_SCOPE)

    exact = service.govern(
        _command(GovernanceAction.SEAL, EntityType.MEMORY, "shared-id")
    )
    assert exact.affected_ids == ("shared-id",)

    unknown_scope = Scope("tenant", "owner", "unknown-subject")
    with pytest.raises(ScopeMismatch, match="shared-id") as mismatch:
        service.govern(
            _command(
                GovernanceAction.SEAL,
                EntityType.MEMORY,
                "shared-id",
                scope=unknown_scope,
            )
        )
    with pytest.raises(EntityNotFound, match="missing-id") as missing:
        service.govern(
            _command(
                GovernanceAction.SEAL,
                EntityType.MEMORY,
                "missing-id",
                scope=unknown_scope,
            )
        )

    assert "private synthetic memory body" not in str(mismatch.value)
    assert "private synthetic memory body" not in str(missing.value)


def test_governance_rejects_invalid_target_action_and_command_fields(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "validation.db")
    service.ingest_event(_event("evt-raw"))

    for action in (
        GovernanceAction.SEAL,
        GovernanceAction.RESTORE,
        GovernanceAction.FORGET,
    ):
        with pytest.raises(ValidationError, match="action"):
            service.govern(_command(action, EntityType.RAW_EVENT, "evt-raw"))

    with pytest.raises(ValidationError, match="reason"):
        _command(
            GovernanceAction.DELETE,
            EntityType.RAW_EVENT,
            "evt-raw",
            reason="   ",
        )
    with pytest.raises(ValidationError, match="occurred_at"):
        _command(
            GovernanceAction.DELETE,
            EntityType.RAW_EVENT,
            "evt-raw",
            occurred_at=NOW.replace(tzinfo=None),
        )
