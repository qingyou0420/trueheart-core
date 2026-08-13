import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import trueheart_core
from examples.basic_memory import main as run_basic_example
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

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_STDOUT = "1 governed memory recalled at clarity 1.00\n"
PUBLIC_NAMES = (
    "AuditRecord",
    "EntityDeleted",
    "EntityNotFound",
    "EntityType",
    "GovernanceAction",
    "GovernanceCommand",
    "GovernanceResult",
    "IdempotencyConflict",
    "InvalidTransition",
    "MemoryDraft",
    "MemoryRecord",
    "MemoryStatus",
    "RawEventDraft",
    "RawEventReceipt",
    "RecallItem",
    "RecallQuery",
    "RepositoryCorruption",
    "RetentionPolicy",
    "SQLiteRepository",
    "Scope",
    "ScopeMismatch",
    "SourceRef",
    "TrueHeart",
    "TrueHeartError",
    "TrustEscalation",
    "TrustLevel",
    "ValidationError",
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
    assert tuple(trueheart_core.__all__) == PUBLIC_NAMES
    assert {getattr(trueheart_core, name) for name in trueheart_core.__all__} == (
        PUBLIC_SYMBOLS
    )


def test_basic_example_cleanup_is_observable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_basic_example(temporary_root=tmp_path)

    captured = capsys.readouterr()
    assert captured.out == EXPECTED_STDOUT
    assert captured.err == ""
    assert list(tmp_path.iterdir()) == []


def test_basic_example_standalone_output_is_strict(tmp_path: Path) -> None:
    example = PROJECT_ROOT / "examples" / "basic_memory.py"

    result = subprocess.run(
        [sys.executable, str(example)],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == EXPECTED_STDOUT
    assert result.stderr == ""
    assert list(tmp_path.iterdir()) == []


def test_readme_five_minute_example_starts_from_a_checkout() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Five-minute example", 1)[1].split("## Lifecycle", 1)[0]
    commands = (
        "git clone https://github.com/qingyou0420/trueheart-core.git",
        "cd trueheart-core",
        'python -m pip install -e ".[dev]"',
        "python examples/basic_memory.py",
    )

    positions = tuple(section.index(command) for command in commands)
    assert positions == tuple(sorted(positions))
    assert "trueheart-core==0.1.0" not in section


def test_code_of_conduct_defines_private_route_and_original_provenance() -> None:
    policy = (PROJECT_ROOT / "CODE_OF_CONDUCT.md").read_text(encoding="utf-8")

    for required_text in (
        "original project policy",
        "MIT license",
        "Security",
        "Advisories",
        "Report a vulnerability",
        "[Conduct]",
        "Publication is blocked until private vulnerability reporting is enabled",
        "GitHub Report abuse",
    ):
        assert required_text in policy


def test_security_guarantees_starts_with_its_introduction() -> None:
    security_guarantees = (PROJECT_ROOT / "docs" / "security-guarantees.md").read_text(
        encoding="utf-8"
    )
    introduction = security_guarantees.splitlines()[2]

    assert introduction.startswith("This document describes TrueHeart Core 0.1.0")


def test_packaging_uses_pep_639_license_metadata() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)

    assert pyproject["build-system"]["requires"] == ["setuptools==84.0.0"]
    assert pyproject["project"]["license"] == "MIT"
    assert pyproject["project"]["license-files"] == ["LICENSE"]
    assert pyproject["project"]["dependencies"] == []
