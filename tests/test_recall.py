import sqlite3
from datetime import UTC, datetime, timedelta, timezone
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


class _TracingSQLiteRepository(SQLiteRepository):
    def __init__(self, path: Path) -> None:
        self.statements: list[str] = []
        super().__init__(path)
        self.statements.clear()

    def _connect(self) -> sqlite3.Connection:
        connection = super()._connect()
        connection.set_trace_callback(self.statements.append)
        return connection


def _service(path: Path) -> TrueHeart:
    return TrueHeart(
        SQLiteRepository(path),
        clock=lambda: datetime(2026, 8, 13, 8, tzinfo=UTC),
    )


def _add_memory(
    service: TrueHeart,
    *,
    memory_id: str,
    event_id: str,
    created_at: datetime,
    scope: Scope = SCOPE,
    kind: str = "fact",
    clear_for: timedelta = timedelta(days=2),
    recall_for: timedelta = timedelta(days=6),
    body: str | None = None,
) -> None:
    service.ingest_event(
        RawEventDraft(
            event_id=event_id,
            scope=scope,
            source=SourceRef(
                source_id=f"source-{event_id}",
                source_type="synthetic",
                occurred_at=datetime(2026, 8, 13, 7, tzinfo=UTC),
                trust=TrustLevel.OBSERVED,
            ),
            content=f"raw private synthetic body {event_id}",
            retention=RetentionPolicy(
                raw_ttl=timedelta(hours=1),
                clear_for=clear_for,
                recall_for=recall_for,
            ),
        )
    )
    service.materialize_once(
        MemoryDraft(
            memory_id=memory_id,
            scope=scope,
            content=body or f"derived synthetic memory {memory_id}",
            source_event_ids=(event_id,),
            kind=kind,
            trust=TrustLevel.OBSERVED,
            created_at=created_at,
        )
    )


def test_recall_isolates_exact_scope_and_optional_kinds(tmp_path: Path) -> None:
    service = _service(tmp_path / "isolation.db")
    other_scope = Scope("tenant", "owner", "other-subject")
    created = datetime(2026, 8, 13, 9, tzinfo=UTC)
    _add_memory(
        service,
        memory_id="mem-fact",
        event_id="evt-fact",
        created_at=created,
    )
    _add_memory(
        service,
        memory_id="mem-plan",
        event_id="evt-plan",
        created_at=created,
        kind="plan",
    )
    _add_memory(
        service,
        memory_id="mem-other",
        event_id="evt-other",
        created_at=created,
        scope=other_scope,
    )

    facts = service.recall(
        RecallQuery(scope=SCOPE, as_of=created + timedelta(days=1), kinds=("fact",))
    )
    all_local = service.recall(
        RecallQuery(scope=SCOPE, as_of=created + timedelta(days=1))
    )

    assert tuple(item.memory.memory_id for item in facts) == ("mem-fact",)
    assert {item.memory.memory_id for item in all_local} == {"mem-fact", "mem-plan"}


def test_recall_excludes_sealed_and_time_expired_memories(tmp_path: Path) -> None:
    path = tmp_path / "eligibility.db"
    service = _service(path)
    created = datetime(2026, 8, 13, 9, tzinfo=UTC)
    _add_memory(
        service, memory_id="mem-active", event_id="evt-active", created_at=created
    )
    _add_memory(
        service, memory_id="mem-sealed", event_id="evt-sealed", created_at=created
    )
    _add_memory(
        service,
        memory_id="mem-expired",
        event_id="evt-expired",
        created_at=created - timedelta(days=7),
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE memories SET status = ? WHERE memory_id = ?",
            ("sealed", "mem-sealed"),
        )

    items = service.recall(RecallQuery(scope=SCOPE, as_of=created))

    assert tuple(item.memory.memory_id for item in items) == ("mem-active",)


@pytest.mark.parametrize(
    ("as_of", "expected"),
    [
        (datetime(2026, 8, 15, 9, tzinfo=UTC), 1.0),
        (datetime(2026, 8, 17, 9, tzinfo=UTC), 0.5),
    ],
)
def test_recall_clarity_at_clear_boundary_and_halfway(
    tmp_path: Path, as_of: datetime, expected: float
) -> None:
    service = _service(tmp_path / f"clarity-{expected}.db")
    _add_memory(
        service,
        memory_id="mem-clarity",
        event_id="evt-clarity",
        created_at=datetime(2026, 8, 13, 9, tzinfo=UTC),
    )

    items = service.recall(RecallQuery(scope=SCOPE, as_of=as_of))

    assert len(items) == 1
    assert items[0].clarity == expected


def test_recall_excludes_memory_exactly_at_recall_until(tmp_path: Path) -> None:
    service = _service(tmp_path / "recall-boundary.db")
    created = datetime(2026, 8, 13, 9, tzinfo=UTC)
    _add_memory(
        service,
        memory_id="mem-boundary",
        event_id="evt-boundary",
        created_at=created,
    )

    assert (
        service.recall(RecallQuery(scope=SCOPE, as_of=created + timedelta(days=6)))
        == ()
    )


def test_recall_orders_before_applying_limit(tmp_path: Path) -> None:
    service = _service(tmp_path / "ordering.db")
    base = datetime(2026, 8, 13, 9, tzinfo=UTC)
    _add_memory(
        service,
        memory_id="mem-faded",
        event_id="evt-faded",
        created_at=base - timedelta(days=3),
    )
    _add_memory(
        service,
        memory_id="mem-z",
        event_id="evt-z",
        created_at=base,
    )
    _add_memory(
        service,
        memory_id="mem-b",
        event_id="evt-b",
        created_at=base + timedelta(hours=1),
    )
    _add_memory(
        service,
        memory_id="mem-a",
        event_id="evt-a",
        created_at=base + timedelta(hours=1),
    )

    items = service.recall(
        RecallQuery(scope=SCOPE, as_of=base + timedelta(days=1), limit=3)
    )

    assert tuple(item.memory.memory_id for item in items) == (
        "mem-a",
        "mem-b",
        "mem-z",
    )


def test_recall_accepts_limits_one_and_one_hundred_and_rejects_bounds(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "limits.db")
    created = datetime(2026, 8, 13, 9, tzinfo=UTC)
    _add_memory(service, memory_id="mem-a", event_id="evt-a", created_at=created)
    _add_memory(service, memory_id="mem-b", event_id="evt-b", created_at=created)

    assert len(service.recall(RecallQuery(scope=SCOPE, as_of=created, limit=1))) == 1
    assert len(service.recall(RecallQuery(scope=SCOPE, as_of=created, limit=100))) == 2
    with pytest.raises(ValidationError, match="limit"):
        RecallQuery(scope=SCOPE, as_of=created, limit=0)
    with pytest.raises(ValidationError, match="limit"):
        RecallQuery(scope=SCOPE, as_of=created, limit=101)


def test_timezone_equivalent_as_of_values_produce_identical_recall(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "timezone.db")
    created = datetime(2026, 8, 13, 9, tzinfo=UTC)
    _add_memory(service, memory_id="mem-time", event_id="evt-time", created_at=created)

    utc_items = service.recall(
        RecallQuery(scope=SCOPE, as_of=datetime(2026, 8, 16, 9, tzinfo=UTC))
    )
    offset_items = service.recall(
        RecallQuery(
            scope=SCOPE,
            as_of=datetime(2026, 8, 16, 17, tzinfo=timezone(timedelta(hours=8))),
        )
    )

    assert offset_items == utc_items


def test_recall_item_never_projects_raw_source_body(tmp_path: Path) -> None:
    service = _service(tmp_path / "body-free.db")
    created = datetime(2026, 8, 13, 9, tzinfo=UTC)
    _add_memory(
        service,
        memory_id="mem-body-free",
        event_id="evt-body-free",
        created_at=created,
        body="derived content allowed in recall",
    )

    item = service.recall(RecallQuery(scope=SCOPE, as_of=created))[0]

    assert item.memory.content == "derived content allowed in recall"
    assert item.memory.source_event_ids == ("evt-body-free",)
    assert not hasattr(item, "raw_content")
    assert not hasattr(item.memory, "raw_content")


def test_recall_translates_corrupt_memory_without_source_body(tmp_path: Path) -> None:
    path = tmp_path / "corrupt-memory.db"
    service = _service(path)
    created = datetime(2026, 8, 13, 9, tzinfo=UTC)
    _add_memory(
        service,
        memory_id="mem-corrupt",
        event_id="evt-corrupt",
        created_at=created,
    )
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE memories SET metadata_json = ? WHERE memory_id = ?",
            ("{private-synthetic-source-body", "mem-corrupt"),
        )

    with pytest.raises(RepositoryCorruption) as error:
        service.recall(RecallQuery(scope=SCOPE, as_of=created))

    assert "private-synthetic-source-body" not in str(error.value)
    assert error.value.__cause__ is None


def test_recall_rejects_corrupt_lineage_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "corrupt-lineage.db"
    service = _service(path)
    created = datetime(2026, 8, 13, 9, tzinfo=UTC)
    _add_memory(
        service,
        memory_id="mem-lineage",
        event_id="evt-lineage",
        created_at=created,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE memories SET dependency_fingerprint = ? WHERE memory_id = ?",
            ("0" * 64, "mem-lineage"),
        )

    with pytest.raises(RepositoryCorruption) as error:
        service.recall(RecallQuery(scope=SCOPE, as_of=created))

    assert error.value.__cause__ is None


def test_recall_rejects_lineage_edge_without_source_receipt(tmp_path: Path) -> None:
    path = tmp_path / "missing-lineage-source.db"
    service = _service(path)
    created = datetime(2026, 8, 13, 9, tzinfo=UTC)
    _add_memory(
        service,
        memory_id="mem-missing-source",
        event_id="evt-missing-source",
        created_at=created,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM raw_events WHERE event_id = ?", ("evt-missing-source",)
        )

    with pytest.raises(RepositoryCorruption) as error:
        service.recall(RecallQuery(scope=SCOPE, as_of=created))

    assert error.value.__cause__ is None


def test_recall_rejects_persisted_trust_above_source_ceiling(tmp_path: Path) -> None:
    path = tmp_path / "corrupt-trust-ceiling.db"
    service = _service(path)
    created = datetime(2026, 8, 13, 9, tzinfo=UTC)
    _add_memory(
        service,
        memory_id="mem-trust",
        event_id="evt-trust",
        created_at=created,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE memories SET trust = ? WHERE memory_id = ?",
            (int(TrustLevel.CONFIRMED), "mem-trust"),
        )

    with pytest.raises(RepositoryCorruption) as error:
        service.recall(RecallQuery(scope=SCOPE, as_of=created))

    assert error.value.__cause__ is None


def test_recall_rejects_persisted_deadlines_not_derived_from_sources(
    tmp_path: Path,
) -> None:
    path = tmp_path / "corrupt-deadlines.db"
    service = _service(path)
    created = datetime(2026, 8, 13, 9, tzinfo=UTC)
    _add_memory(
        service,
        memory_id="mem-deadline",
        event_id="evt-deadline",
        created_at=created,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE memories SET clear_until = ?, recall_until = ? WHERE memory_id = ?",
            (
                "2026-08-20T09:00:00.000000+00:00",
                "2126-08-20T09:00:00.000000+00:00",
                "mem-deadline",
            ),
        )

    with pytest.raises(RepositoryCorruption) as error:
        service.recall(RecallQuery(scope=SCOPE, as_of=created))

    assert error.value.__cause__ is None


def test_recall_uses_one_read_snapshot_and_batch_loads_lineage(
    tmp_path: Path,
) -> None:
    repository = _TracingSQLiteRepository(tmp_path / "batched-recall.db")
    service = TrueHeart(
        repository,
        clock=lambda: datetime(2026, 8, 13, 8, tzinfo=UTC),
    )
    created = datetime(2026, 8, 13, 9, tzinfo=UTC)
    for suffix in ("a", "b", "c"):
        _add_memory(
            service,
            memory_id=f"mem-{suffix}",
            event_id=f"evt-{suffix}",
            created_at=created,
        )
    repository.statements.clear()

    items = service.recall(RecallQuery(scope=SCOPE, as_of=created))

    assert len(items) == 3
    statements = [statement.upper() for statement in repository.statements]
    assert sum(statement.startswith("BEGIN") for statement in statements) == 1
    assert sum(statement.startswith("COMMIT") for statement in statements) == 1
    selects = [statement for statement in statements if statement.startswith("SELECT")]
    assert len(selects) == 2
    assert sum("JOIN MEMORIES" in statement for statement in selects) == 1
    assert sum("FROM MEMORY_SOURCES" in statement for statement in selects) == 1


def test_empty_recall_checks_schema_without_right_join(tmp_path: Path) -> None:
    repository = _TracingSQLiteRepository(tmp_path / "empty-recall.db")
    repository.statements.clear()

    items = TrueHeart(repository).recall(
        RecallQuery(scope=SCOPE, as_of=datetime(2026, 8, 13, 9, tzinfo=UTC))
    )

    assert items == ()
    statements = [statement.upper() for statement in repository.statements]
    selects = [statement for statement in statements if statement.startswith("SELECT")]
    assert len(selects) == 2
    recall_selects = [
        statement for statement in selects if "FROM SCHEMA_MIGRATIONS" in statement
    ]
    assert len(recall_selects) == 1
    assert "LEFT JOIN MEMORIES" in recall_selects[0]
    assert "RIGHT JOIN" not in recall_selects[0]
    assert sum("FROM MEMORY_SOURCES" in statement for statement in selects) == 1


@pytest.mark.parametrize(
    ("column", "stored_value"),
    [
        ("status", "private-status-sentinel"),
        ("clear_until", "private-time-sentinel"),
        ("recall_until", "private-time-sentinel"),
        ("clear_until", "2026-08-15T17:00:00.000000+08:00"),
        ("recall_until", "2026-08-14T09:00:00.000000+00:00"),
    ],
    ids=[
        "status",
        "malformed-clear-until",
        "malformed-recall-until",
        "noncanonical-offset",
        "invalid-order",
    ],
)
def test_recall_rejects_corrupt_status_or_deadline_before_eligibility_filtering(
    tmp_path: Path,
    column: str,
    stored_value: str,
) -> None:
    path = tmp_path / f"corrupt-filtered-{column}.db"
    service = _service(path)
    created = datetime(2026, 8, 13, 9, tzinfo=UTC)
    _add_memory(
        service,
        memory_id="mem-corrupt-filter",
        event_id="evt-corrupt-filter",
        created_at=created,
    )
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            f"UPDATE memories SET {column} = ? WHERE memory_id = ?",
            (stored_value, "mem-corrupt-filter"),
        )

    with pytest.raises(RepositoryCorruption) as error:
        service.recall(RecallQuery(scope=SCOPE, as_of=created + timedelta(days=100)))

    assert stored_value not in str(error.value)
    assert "raw private synthetic body" not in str(error.value)
    assert error.value.__cause__ is None


def test_recall_validates_corrupt_kind_before_optional_kind_filter(
    tmp_path: Path,
) -> None:
    path = tmp_path / "corrupt-filtered-kind.db"
    service = _service(path)
    created = datetime(2026, 8, 13, 9, tzinfo=UTC)
    _add_memory(
        service,
        memory_id="mem-corrupt-kind",
        event_id="evt-corrupt-kind",
        created_at=created,
        kind="plan",
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE memories SET kind = ? WHERE memory_id = ?",
            (sqlite3.Binary(b"private-kind-sentinel"), "mem-corrupt-kind"),
        )

    query = RecallQuery(scope=SCOPE, as_of=created, kinds=("fact",))
    with pytest.raises(RepositoryCorruption) as error:
        service.recall(query)

    assert "private-kind-sentinel" not in str(error.value)
    assert error.value.__cause__ is None


def test_recall_translates_corrupt_lineage_grouping_key_without_body(
    tmp_path: Path,
) -> None:
    path = tmp_path / "corrupt-lineage-group.db"
    service = _service(path)
    created = datetime(2026, 8, 13, 9, tzinfo=UTC)
    _add_memory(
        service,
        memory_id="mem-group",
        event_id="evt-group",
        created_at=created,
    )
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "UPDATE memory_sources SET memory_id = ? WHERE memory_id = ?",
            (sqlite3.Binary(b"private-grouping-sentinel"), "mem-group"),
        )

    with pytest.raises(RepositoryCorruption) as error:
        service.recall(RecallQuery(scope=SCOPE, as_of=created))

    assert "private-grouping-sentinel" not in str(error.value)
    assert "raw private synthetic body" not in str(error.value)
    assert error.value.__cause__ is None
