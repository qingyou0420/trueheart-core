import subprocess
import sys
from pathlib import Path

import trueheart_core
from trueheart_core import (
    AuditRecord,
    EntityDeleted,
    EntityNotFound,
    EntityType,
    GovernanceAction,
    GovernanceCommand,
    GovernanceResult,
    IdempotencyConflict,
    InvalidTransition,
    MemoryDraft,
    MemoryRecord,
    MemoryStatus,
    RawEventDraft,
    RawEventReceipt,
    RecallItem,
    RecallQuery,
    RepositoryCorruption,
    RetentionPolicy,
    Scope,
    ScopeMismatch,
    SourceRef,
    SQLiteRepository,
    TrueHeart,
    TrueHeartError,
    TrustEscalation,
    TrustLevel,
    ValidationError,
)

PUBLIC_SYMBOLS = {
    AuditRecord,
    EntityDeleted,
    EntityNotFound,
    EntityType,
    GovernanceAction,
    GovernanceCommand,
    GovernanceResult,
    IdempotencyConflict,
    InvalidTransition,
    MemoryDraft,
    MemoryRecord,
    MemoryStatus,
    RawEventDraft,
    RawEventReceipt,
    RecallItem,
    RecallQuery,
    RepositoryCorruption,
    RetentionPolicy,
    Scope,
    ScopeMismatch,
    SourceRef,
    SQLiteRepository,
    TrueHeart,
    TrueHeartError,
    TrustEscalation,
    TrustLevel,
    ValidationError,
}


def test_public_api_exports_only_supported_symbols() -> None:
    assert {getattr(trueheart_core, name) for name in trueheart_core.__all__} == (
        PUBLIC_SYMBOLS
    )


def test_basic_example_runs_without_leaving_database_files(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [sys.executable, str(project_root / "examples" / "basic_memory.py")],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "1 governed memory recalled at clarity 1.00\n"
    assert list(tmp_path.iterdir()) == []
