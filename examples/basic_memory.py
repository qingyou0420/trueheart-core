"""Run a synthetic, local-only TrueHeart Core memory lifecycle."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from trueheart_core import (
    MemoryDraft,
    RawEventDraft,
    RecallQuery,
    RetentionPolicy,
    Scope,
    SourceRef,
    SQLiteRepository,
    TrueHeart,
    TrustLevel,
)


def main() -> None:
    now = datetime(2026, 8, 13, 12, tzinfo=UTC)
    scope = Scope("synthetic-tenant", "synthetic-owner", "synthetic-subject")

    with TemporaryDirectory() as temporary_directory:
        service = TrueHeart(
            SQLiteRepository(Path(temporary_directory) / "memory.db"),
            clock=lambda: now,
        )
        service.ingest_event(
            RawEventDraft(
                event_id="event-1",
                scope=scope,
                source=SourceRef(
                    source_id="example-source",
                    source_type="synthetic",
                    occurred_at=now,
                    trust=TrustLevel.OBSERVED,
                ),
                content="A synthetic preference was observed.",
                retention=RetentionPolicy(
                    raw_ttl=timedelta(days=7),
                    clear_for=timedelta(days=30),
                    recall_for=timedelta(days=90),
                ),
            )
        )
        service.materialize_once(
            MemoryDraft(
                memory_id="memory-1",
                scope=scope,
                content="The synthetic subject prefers concise summaries.",
                source_event_ids=("event-1",),
                kind="preference",
                trust=TrustLevel.OBSERVED,
                created_at=now,
            )
        )
        recalled = service.recall(RecallQuery(scope=scope, as_of=now))

        print(
            f"{len(recalled)} governed memory recalled "
            f"at clarity {recalled[0].clarity:.2f}"
        )


if __name__ == "__main__":
    main()
