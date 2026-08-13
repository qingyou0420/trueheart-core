from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone, tzinfo

import pytest

from trueheart_core import (
    AuditRecord,
    EntityType,
    MemoryRecord,
    MemoryStatus,
    RawEventDraft,
    RawEventReceipt,
    RetentionPolicy,
    Scope,
    SourceRef,
    TrustLevel,
    ValidationError,
)

TZINFO_SENTINEL = "PRIVATE-TZINFO-SENTINEL-42"


class _TypeErrorTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        del dt
        raise TypeError(TZINFO_SENTINEL)

    def dst(self, dt: datetime | None) -> timedelta | None:
        del dt
        return None


def test_scope_rejects_blank_or_oversized_components() -> None:
    with pytest.raises(ValidationError, match="tenant_id"):
        Scope(" ", "owner", "subject")
    with pytest.raises(ValidationError, match="subject_id"):
        Scope("tenant", "owner", "s" * 129)


def test_event_normalizes_time_and_freezes_metadata() -> None:
    source_metadata = {"channel": ["chat"]}
    draft = RawEventDraft(
        event_id="evt-1",
        scope=Scope("tenant", "owner", "subject"),
        source=SourceRef(
            source_id="message-1",
            source_type="conversation",
            occurred_at=datetime(2026, 8, 13, 9, tzinfo=UTC),
            trust=TrustLevel.OBSERVED,
            metadata=source_metadata,
        ),
        content="synthetic message",
        retention=RetentionPolicy(
            raw_ttl=timedelta(days=7),
            clear_for=timedelta(days=7),
            recall_for=timedelta(days=30),
        ),
        metadata={"labels": ["example"]},
    )
    source_metadata["channel"].append("mutated")
    assert draft.source.occurred_at.tzinfo is UTC
    assert tuple(draft.source.metadata["channel"]) == ("chat",)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object()])
def test_metadata_rejects_non_json_values(value: object) -> None:
    with pytest.raises(ValidationError, match="metadata"):
        SourceRef(
            source_id="source",
            source_type="test",
            occurred_at=datetime.now(UTC),
            trust=TrustLevel.UNTRUSTED,
            metadata={"bad": value},
        )


def test_datetime_rejects_naive_values() -> None:
    with pytest.raises(ValidationError, match="occurred_at"):
        SourceRef(
            source_id="source",
            source_type="test",
            occurred_at=datetime(2026, 8, 13, 9, tzinfo=UTC).replace(tzinfo=None),
            trust=TrustLevel.UNTRUSTED,
        )


def test_datetime_normalization_contains_offset_boundary_overflow() -> None:
    boundary = datetime.max.replace(tzinfo=timezone(-timedelta(hours=23, minutes=59)))

    with pytest.raises(ValidationError, match="occurred_at") as error:
        SourceRef(
            source_id="source",
            source_type="test",
            occurred_at=boundary,
            trust=TrustLevel.UNTRUSTED,
        )

    assert str(boundary) not in str(error.value)
    assert error.value.__cause__ is None


def test_datetime_normalization_contains_custom_tzinfo_type_error() -> None:
    hostile_datetime = datetime(2026, 8, 13, 9, tzinfo=_TypeErrorTimezone())

    with pytest.raises(ValidationError, match="occurred_at") as error:
        SourceRef(
            source_id="source",
            source_type="test",
            occurred_at=hostile_datetime,
            trust=TrustLevel.UNTRUSTED,
        )

    assert TZINFO_SENTINEL not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize(
    "content",
    ["", "x" * (256 * 1024 + 1)],
    ids=["empty", "oversized"],
)
def test_event_rejects_empty_or_oversized_content(content: str) -> None:
    with pytest.raises(ValidationError, match="content"):
        RawEventDraft(
            event_id="evt-1",
            scope=Scope("tenant", "owner", "subject"),
            source=SourceRef(
                source_id="source",
                source_type="test",
                occurred_at=datetime.now(UTC),
                trust=TrustLevel.UNTRUSTED,
            ),
            content=content,
            retention=RetentionPolicy(
                raw_ttl=timedelta(days=1),
                clear_for=timedelta(days=1),
                recall_for=timedelta(days=2),
            ),
        )


def test_metadata_rejects_serialized_value_over_16_kib() -> None:
    with pytest.raises(ValidationError, match="metadata"):
        SourceRef(
            source_id="source",
            source_type="test",
            occurred_at=datetime.now(UTC),
            trust=TrustLevel.UNTRUSTED,
            metadata={"payload": "x" * (16 * 1024)},
        )


def _nested_metadata(depth: int) -> dict[str, object]:
    value: object = "synthetic leaf"
    for _ in range(depth - 1):
        value = {"nested": value}
    return {"root": value}


def test_metadata_accepts_depth_64_and_rejects_depth_65_without_leakage() -> None:
    accepted = SourceRef(
        source_id="source",
        source_type="test",
        occurred_at=datetime.now(UTC),
        trust=TrustLevel.UNTRUSTED,
        metadata=_nested_metadata(64),  # type: ignore[arg-type]
    )

    assert accepted.metadata
    with pytest.raises(ValidationError, match="metadata") as error:
        SourceRef(
            source_id="source",
            source_type="test",
            occurred_at=datetime.now(UTC),
            trust=TrustLevel.UNTRUSTED,
            metadata=_nested_metadata(65),  # type: ignore[arg-type]
        )

    assert "synthetic leaf" not in str(error.value)
    assert error.value.__cause__ is None


def test_metadata_accepts_4096_bit_integer_and_rejects_4097_bits() -> None:
    accepted_value = 1 << 4095
    accepted = SourceRef(
        source_id="source",
        source_type="test",
        occurred_at=datetime.now(UTC),
        trust=TrustLevel.UNTRUSTED,
        metadata={"number": accepted_value},
    )

    assert accepted.metadata["number"] == accepted_value
    rejected_value = 1 << 4096
    with pytest.raises(ValidationError, match="metadata") as error:
        SourceRef(
            source_id="source",
            source_type="test",
            occurred_at=datetime.now(UTC),
            trust=TrustLevel.UNTRUSTED,
            metadata={"number": rejected_value},
        )

    assert str(rejected_value) not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize(
    "metadata",
    [{"number": 10**5000}, _nested_metadata(1100)],
    ids=["huge-integer", "extreme-depth"],
)
def test_adversarial_metadata_failures_are_contained(
    metadata: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="metadata") as error:
        SourceRef(
            source_id="source",
            source_type="test",
            occurred_at=datetime.now(UTC),
            trust=TrustLevel.UNTRUSTED,
            metadata=metadata,  # type: ignore[arg-type]
        )

    assert error.value.__cause__ is None


@pytest.mark.parametrize("duration", [timedelta(0), timedelta(days=-1)])
def test_retention_rejects_non_positive_durations(duration: timedelta) -> None:
    with pytest.raises(ValidationError, match="raw_ttl"):
        RetentionPolicy(
            raw_ttl=duration,
            clear_for=timedelta(days=1),
            recall_for=timedelta(days=2),
        )


def test_retention_rejects_recall_before_clear() -> None:
    with pytest.raises(ValidationError, match="recall_for"):
        RetentionPolicy(
            raw_ttl=timedelta(days=1),
            clear_for=timedelta(days=2),
            recall_for=timedelta(days=1),
        )


@pytest.mark.parametrize("field", ["content", "metadata"])
def test_unencodable_text_raises_body_free_validation_error(field: str) -> None:
    kwargs: dict[str, object] = {
        "event_id": "evt-1",
        "scope": Scope("tenant", "owner", "subject"),
        "source": SourceRef(
            source_id="source",
            source_type="test",
            occurred_at=datetime.now(UTC),
            trust=TrustLevel.UNTRUSTED,
        ),
        "content": "synthetic message",
        "retention": RetentionPolicy(
            raw_ttl=timedelta(days=1),
            clear_for=timedelta(days=1),
            recall_for=timedelta(days=2),
        ),
        "metadata": {},
    }
    malformed = "\ud800"
    if field == "content":
        kwargs["content"] = malformed
    else:
        kwargs["metadata"] = {"bad": malformed}

    with pytest.raises(ValidationError, match=field) as error:
        RawEventDraft(**kwargs)  # type: ignore[arg-type]

    assert malformed not in str(error.value)


def test_raw_event_receipt_validates_and_normalizes_public_fields() -> None:
    metadata = {"tags": ["synthetic"]}
    receipt = RawEventReceipt(
        event_id="evt-1",
        scope=Scope("tenant", "owner", "subject"),
        source=SourceRef(
            source_id="source",
            source_type="test",
            occurred_at=datetime(2026, 8, 13, 9, tzinfo=UTC),
            trust=TrustLevel.OBSERVED,
        ),
        content_hash="hash",
        ingested_at=datetime(2026, 8, 13, 17, tzinfo=UTC),
        raw_expires_at=datetime(2026, 8, 14, 17, tzinfo=UTC),
        clear_for=timedelta(days=1),
        recall_for=timedelta(days=2),
        content_available=True,
        metadata=metadata,
    )
    metadata["tags"].append("mutated")

    assert receipt.ingested_at.tzinfo is UTC
    assert receipt.raw_expires_at.tzinfo is UTC
    assert tuple(receipt.metadata["tags"]) == ("synthetic",)
    with pytest.raises(ValidationError, match="ingested_at"):
        replace(
            receipt,
            ingested_at=datetime(2026, 8, 13, 17, tzinfo=UTC).replace(tzinfo=None),
        )


def test_memory_record_rejects_invalid_content_and_normalizes_datetimes() -> None:
    record = MemoryRecord(
        memory_id="mem-1",
        scope=Scope("tenant", "owner", "subject"),
        content="synthetic memory",
        source_event_ids=("evt-1",),
        dependency_fingerprint="fingerprint",
        kind="fact",
        trust=TrustLevel.OBSERVED,
        created_at=datetime(2026, 8, 13, 17, tzinfo=timezone(timedelta(hours=8))),
        clear_until=datetime(2026, 8, 14, 17, tzinfo=timezone(timedelta(hours=8))),
        recall_until=datetime(2026, 8, 15, 17, tzinfo=timezone(timedelta(hours=8))),
        status=MemoryStatus.ACTIVE,
        metadata={"labels": ["synthetic"]},
    )
    assert record.created_at.tzinfo is UTC
    assert record.clear_until.tzinfo is UTC
    assert record.recall_until.tzinfo is UTC
    assert tuple(record.metadata["labels"]) == ("synthetic",)
    with pytest.raises(ValidationError, match="content"):
        replace(record, content="")


def test_audit_record_rejects_naive_datetime_and_oversized_metadata() -> None:
    metadata = {"labels": ["synthetic"]}
    kwargs = {
        "audit_id": "audit-1",
        "scope": Scope("tenant", "owner", "subject"),
        "action": "ingest",
        "entity_type": EntityType.RAW_EVENT,
        "entity_id": "evt-1",
        "occurred_at": datetime(2026, 8, 13, 9, tzinfo=UTC),
        "reason": "synthetic reason",
        "metadata": metadata,
    }
    audit = AuditRecord(**kwargs)
    metadata["labels"].append("mutated")
    assert tuple(audit.metadata["labels"]) == ("synthetic",)
    with pytest.raises(ValidationError, match="occurred_at"):
        AuditRecord(
            **{
                **kwargs,
                "occurred_at": datetime(2026, 8, 13, 9, tzinfo=UTC).replace(
                    tzinfo=None
                ),
            }
        )
    with pytest.raises(ValidationError, match="metadata"):
        AuditRecord(**{**kwargs, "metadata": {"payload": "x" * (16 * 1024)}})
