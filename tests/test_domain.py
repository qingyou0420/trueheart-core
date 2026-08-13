from datetime import UTC, datetime, timedelta

import pytest

from trueheart_core import (
    RawEventDraft,
    RetentionPolicy,
    Scope,
    SourceRef,
    TrustLevel,
    ValidationError,
)


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
