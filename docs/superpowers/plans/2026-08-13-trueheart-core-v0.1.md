# TrueHeart Core v0.1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish an application-ready v0.1 Python library for scoped, provenance-aware, transactional agent memory governance.

**Architecture:** Immutable domain contracts feed a synchronous `TrueHeart` service, which delegates atomic persistence to a narrow `Repository` protocol. The first adapter uses standard-library SQLite with normalized tables for raw bodies, derived memories, lineage, tombstones, and body-free audit records.

**Tech Stack:** Python 3.11+, `dataclasses`, `enum`, `hashlib`, `json`, `sqlite3`, `pytest`, Ruff, mypy, setuptools, GitHub Actions.

## Global Constraints

- Implement from tests without copying code, tests, prose, fixtures, or history from either NightForest repository.
- Runtime dependencies remain empty; the core performs no network, model, plugin, shell, vector, or background-worker operation.
- All public operations require an exact `Scope(tenant_id, owner_id, subject_id)` and fail closed on mismatch.
- All public datetimes are timezone-aware and normalized to UTC.
- Metadata is JSON-only and at most 16 KiB serialized; content is non-empty and at most 256 KiB encoded as UTF-8.
- SQLite is plaintext local storage; documentation must not claim encryption, hostile-host protection, provider deletion, or arbitrary-backup erasure.
- Forget and delete remove library-owned bodies atomically and retain body-free tombstones that block identifier or dependency resurrection.
- Tests use synthetic values only and never access NightForest files, data, prompts, characters, databases, or configuration.
- Each production behavior follows RED, observed failure, minimal GREEN, and fresh passing verification.
- Version is `0.1.0`; license is MIT with copyright `2026 qingyou0420`.

---

## File map

- `src/trueheart_core/domain.py`: immutable public DTOs, enums, limits, UTC normalization, deep-frozen metadata.
- `src/trueheart_core/errors.py`: stable public exceptions without body-bearing messages.
- `src/trueheart_core/ports.py`: the narrow persistence protocol used by `TrueHeart`.
- `src/trueheart_core/service.py`: lifecycle validation, clarity, trust ceiling, and high-level API.
- `src/trueheart_core/sqlite.py`: schema v1, transactions, projections, lineage, tombstones, and audit persistence.
- `src/trueheart_core/__init__.py`: explicit supported public API.
- `tests/`: contract tests through public APIs and narrowly scoped adapter corruption tests.
- `examples/basic_memory.py`: executable end-to-end example using only synthetic text.
- `docs/`: architecture, security guarantees, and threat model.
- `.github/`: pinned test/security workflows and contribution templates.

### Task 1: Repository foundation and domain contracts

**Files:**
- Create: `AGENTS.md`
- Create: `.gitignore`
- Create: `LICENSE`
- Create: `pyproject.toml`
- Create: `src/trueheart_core/__init__.py`
- Create: `src/trueheart_core/domain.py`
- Create: `src/trueheart_core/errors.py`
- Create: `tests/test_domain.py`

**Interfaces:**
- Produces: the public DTO and exception names consumed by every later task.
- Consumes: no earlier implementation.

- [ ] **Step 1: Add the packaging and governance files**

`pyproject.toml` must declare distribution `trueheart-core`, version `0.1.0`,
Python `>=3.11`, no runtime dependencies, setuptools `src` discovery, and a
`dev` extra containing `build>=1.2,<2`, `mypy>=1.15,<2`, `pytest>=8,<9`, and
`ruff>=0.11,<1`. Configure pytest for `tests`, Ruff for Python 3.11 with line
length 88, and mypy with `strict = true`.

`AGENTS.md` must restate the Global Constraints, require TDD, forbid copied
NightForest content and secrets, and require separate authorization for push,
tag, PyPI publication, or GitHub Release. `.gitignore` must cover Python caches,
virtual environments, build artifacts, coverage, local databases, IDE files,
and `.superpowers/`.

- [ ] **Step 2: Write the first failing domain tests**

Create `tests/test_domain.py` with literal expectations for:

```python
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
```

Add separate tests for naive datetimes, empty and 256-KiB-plus content, metadata
over 16 KiB, non-positive retention, and `recall_for < clear_for`.

- [ ] **Step 3: Run the domain test and observe RED**

Run:

```powershell
python -m pip install -e ".[dev]"
python -m pytest tests/test_domain.py -q
```

Expected: collection fails because the `trueheart_core` public contracts do not
exist yet. Installation may succeed with an empty namespace; that does not count
as GREEN.

- [ ] **Step 4: Implement the minimal domain and error layer**

Use frozen, slotted dataclasses and these exact public names:

```python
class TrustLevel(IntEnum):
    UNTRUSTED = 0
    OBSERVED = 1
    CONFIRMED = 2

class EntityType(StrEnum):
    RAW_EVENT = "raw_event"
    MEMORY = "memory"

class GovernanceAction(StrEnum):
    SEAL = "seal"
    RESTORE = "restore"
    FORGET = "forget"
    DELETE = "delete"

class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SEALED = "sealed"

@dataclass(frozen=True, slots=True)
class Scope:
    tenant_id: str
    owner_id: str
    subject_id: str

@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    raw_ttl: timedelta
    clear_for: timedelta
    recall_for: timedelta

@dataclass(frozen=True, slots=True)
class SourceRef:
    source_id: str
    source_type: str
    occurred_at: datetime
    trust: TrustLevel
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

@dataclass(frozen=True, slots=True)
class RawEventDraft:
    event_id: str
    scope: Scope
    source: SourceRef
    content: str
    retention: RetentionPolicy
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

@dataclass(frozen=True, slots=True)
class MemoryDraft:
    memory_id: str
    scope: Scope
    content: str
    source_event_ids: tuple[str, ...]
    kind: str
    trust: TrustLevel
    created_at: datetime
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

@dataclass(frozen=True, slots=True)
class RecallQuery:
    scope: Scope
    as_of: datetime
    limit: int = 20
    kinds: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class GovernanceCommand:
    scope: Scope
    action: GovernanceAction
    entity_type: EntityType
    entity_id: str
    occurred_at: datetime
    reason: str

@dataclass(frozen=True, slots=True)
class RawEventReceipt:
    event_id: str
    scope: Scope
    source: SourceRef
    content_hash: str
    ingested_at: datetime
    raw_expires_at: datetime
    clear_for: timedelta
    recall_for: timedelta
    content_available: bool
    metadata: Mapping[str, JsonValue]

@dataclass(frozen=True, slots=True)
class MemoryRecord:
    memory_id: str
    scope: Scope
    content: str
    source_event_ids: tuple[str, ...]
    dependency_fingerprint: str
    kind: str
    trust: TrustLevel
    created_at: datetime
    clear_until: datetime
    recall_until: datetime
    status: MemoryStatus
    metadata: Mapping[str, JsonValue]

@dataclass(frozen=True, slots=True)
class RecallItem:
    memory: MemoryRecord
    clarity: float

@dataclass(frozen=True, slots=True)
class GovernanceResult:
    command: GovernanceCommand
    affected_ids: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class AuditRecord:
    audit_id: str
    scope: Scope
    action: str
    entity_type: EntityType
    entity_id: str
    occurred_at: datetime
    reason: str
    metadata: Mapping[str, JsonValue]
```

Implement private helpers that deep-copy and freeze JSON objects, normalize time
to `UTC`, and produce canonical JSON. Validation errors name only the field and
rule, never rejected bodies.

`errors.py` must export `TrueHeartError`, `ValidationError`, `EntityNotFound`,
`ScopeMismatch`, `IdempotencyConflict`, `EntityDeleted`, `InvalidTransition`,
`TrustEscalation`, and `RepositoryCorruption`. Each constructor accepts only
identifiers, field names, or fixed diagnostic text; none accepts a body value.

- [ ] **Step 5: Verify GREEN and static checks**

Run:

```powershell
python -m pytest tests/test_domain.py -q
python -m ruff check src/trueheart_core/domain.py src/trueheart_core/errors.py tests/test_domain.py
python -m ruff format --check src/trueheart_core/domain.py src/trueheart_core/errors.py tests/test_domain.py
python -m mypy src/trueheart_core
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit the foundation**

```powershell
git add AGENTS.md .gitignore LICENSE pyproject.toml src tests/test_domain.py
git commit -m "feat: define TrueHeart Core contracts"
```

### Task 2: SQLite schema and idempotent event ingest

**Files:**
- Create: `src/trueheart_core/ports.py`
- Create: `src/trueheart_core/sqlite.py`
- Create: `src/trueheart_core/service.py`
- Create: `tests/test_ingest.py`

**Interfaces:**
- Consumes: Task 1 DTOs and errors.
- Produces: `Repository`, `SQLiteRepository`, `TrueHeart.ingest_event()`, and schema version 1.

`ports.py` starts with this protocol and Tasks 3-4 add the remaining methods
without changing existing signatures:

```python
class Repository(Protocol):
    def ingest_event(
        self,
        draft: RawEventDraft,
        *,
        content_hash: str,
        ingested_at: datetime,
        raw_expires_at: datetime,
    ) -> RawEventReceipt: ...

    def materialize_once(
        self,
        draft: MemoryDraft,
        *,
        dependency_fingerprint: str,
    ) -> MemoryRecord: ...

    def recall_candidates(
        self,
        scope: Scope,
        *,
        as_of: datetime,
        kinds: tuple[str, ...],
    ) -> tuple[MemoryRecord, ...]: ...

    def expire_raw_content(self, *, as_of: datetime) -> int: ...
    def govern(self, command: GovernanceCommand) -> GovernanceResult: ...
    def audit(self, scope: Scope, *, limit: int) -> tuple[AuditRecord, ...]: ...
```

High-level adapter methods own transaction boundaries and re-check lifecycle
invariants inside those transactions. `TrueHeart` owns public validation,
canonical hashes/fingerprints, clarity calculation, deterministic sorting, and
body-free exception translation.

- [ ] **Step 1: Write ingest contract tests one behavior at a time**

Use a temporary file-backed database and construct `TrueHeart(SQLiteRepository(path))`.
Add these tests with literal expected values:

- `test_ingest_returns_body_free_receipt_and_persists_across_instances`;
- `test_identical_ingest_is_idempotent`;
- `test_same_event_id_with_changed_body_raises_conflict`;
- `test_same_event_id_in_another_scope_is_independent`;
- `test_tombstoned_event_id_cannot_be_reused` using a preloaded body-free
  tombstone fixture inserted with parameterized SQL;
- `test_schema_version_newer_than_supported_fails_closed`;
- `test_memory_database_path_is_rejected`.

The first test must assert the SHA-256 literal for `b"synthetic message"`:

```python
assert receipt.content_hash == (
    "5f241eec5564d5a9b1aa6adf128bdbbdb5529ad99af91e7cd2ded2107e5ea3e2"
)
assert not hasattr(receipt, "content")
```

- [ ] **Step 2: Run each new test before its implementation**

Run these nodes sequentially, adding only the minimal production behavior after
each observed failure:

```powershell
python -m pytest tests/test_ingest.py::test_ingest_returns_body_free_receipt_and_persists_across_instances -q
python -m pytest tests/test_ingest.py::test_identical_ingest_is_idempotent -q
python -m pytest tests/test_ingest.py::test_same_event_id_with_changed_body_raises_conflict -q
python -m pytest tests/test_ingest.py::test_same_event_id_in_another_scope_is_independent -q
python -m pytest tests/test_ingest.py::test_tombstoned_event_id_cannot_be_reused -q
python -m pytest tests/test_ingest.py::test_schema_version_newer_than_supported_fails_closed -q
python -m pytest tests/test_ingest.py::test_memory_database_path_is_rejected -q
```

Expected RED progresses from missing `SQLiteRepository` to missing schema and
then the named contract failure. Record the observed failure in the task report
before adding the production branch that satisfies it.

- [ ] **Step 3: Implement schema version 1 and ingest**

`SQLiteRepository.__init__(path: str | Path)` rejects `":memory:"`, creates the
parent directory, and initializes schema version 1. Every connection enables
foreign keys, uses `sqlite3.Row`, and enables WAL for a file-backed database.

Use composite scope keys in all entity tables. `raw_events` stores source and
retention metadata plus SHA-256, while `raw_event_content` alone stores the
plaintext body. `tombstones` and `audit_log` contain no body column. Add
`CHECK(json_valid(...))` to JSON columns.

`TrueHeart.ingest_event()` passes a UTC ingest time from an injectable clock:

```python
class TrueHeart:
    def __init__(
        self,
        repository: Repository,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None: ...
```

Ingest computes canonical hashes and dates before one `BEGIN IMMEDIATE`
transaction. Identical canonical fields return the existing receipt without a
second audit row; any difference raises `IdempotencyConflict`. A tombstone raises
`EntityDeleted`.

- [ ] **Step 4: Verify ingest GREEN**

```powershell
python -m pytest tests/test_domain.py tests/test_ingest.py -q
python -m ruff check src/trueheart_core tests/test_domain.py tests/test_ingest.py
python -m ruff format --check src/trueheart_core tests/test_domain.py tests/test_ingest.py
python -m mypy src/trueheart_core
git diff --check
```

Expected: all commands exit 0 and audit rows contain identifiers/action metadata
only.

- [ ] **Step 5: Commit ingest**

```powershell
git add src/trueheart_core tests/test_ingest.py
git commit -m "feat: add idempotent event ingest"
```

### Task 3: Atomic materialization and deterministic recall

**Files:**
- Modify: `src/trueheart_core/ports.py`
- Modify: `src/trueheart_core/sqlite.py`
- Modify: `src/trueheart_core/service.py`
- Create: `tests/test_materialize.py`
- Create: `tests/test_recall.py`

**Interfaces:**
- Consumes: stored event receipts and policies from Task 2.
- Produces: `TrueHeart.materialize_once()`, `TrueHeart.recall()`, lineage edges, and pure clarity computation.

- [ ] **Step 1: Write materialization RED tests**

Add and run individually:

- `test_materialize_requires_at_least_one_source`;
- `test_materialize_rejects_missing_or_cross_scope_sources`;
- `test_materialize_cannot_exceed_least_trusted_source`;
- `test_materialize_writes_memory_and_all_edges_atomically`;
- `test_identical_materialization_is_idempotent`;
- `test_changed_memory_with_same_id_conflicts`;
- `test_tombstoned_id_or_dependency_fingerprint_cannot_rematerialize`.

Dependency fingerprint is SHA-256 over canonical JSON containing exact scope,
memory kind, and sorted unique source event IDs. Duplicate source IDs are
rejected rather than silently normalized.

- [ ] **Step 2: Implement minimal materialization**

In one `BEGIN IMMEDIATE` transaction, load all sources by exact scope, calculate
the minimum trust and shortest `clear_for`/`recall_for`, reject trust escalation,
and insert the memory plus edges. Set:

```python
clear_until = draft.created_at + shortest_clear_for
recall_until = draft.created_at + shortest_recall_for
```

Identical repeated materialization returns the existing projection without a
new audit row. A conflicting ID, deleted ID, or deleted dependency fingerprint
fails closed.

- [ ] **Step 3: Write recall RED tests**

Add literal scenarios for:

- exact scope and optional kind isolation;
- sealed and time-expired memory exclusion;
- clarity exactly `1.0` at `clear_until`, `0.5` halfway through fading, and
  exclusion at `recall_until`;
- ordering by clarity descending, created time descending, memory ID ascending;
- limits 1 and 100 plus rejection of 0 and 101;
- timezone-equivalent `as_of` values producing identical results;
- no raw source body in `RecallItem`.

- [ ] **Step 4: Implement recall and clarity**

Expose a pure internal `_clarity(memory, as_of) -> float`. Repository recall
selects eligible active records for the exact scope and optional kinds. Service
computes clarity, applies the specified stable ordering, and slices only after
sorting.

- [ ] **Step 5: Verify materialization and recall GREEN**

```powershell
python -m pytest tests/test_domain.py tests/test_ingest.py tests/test_materialize.py tests/test_recall.py -q
python -m ruff check src/trueheart_core tests
python -m ruff format --check src/trueheart_core tests
python -m mypy src/trueheart_core
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit materialization and recall**

```powershell
git add src/trueheart_core tests/test_materialize.py tests/test_recall.py
git commit -m "feat: add governed materialization and recall"
```

### Task 4: Retention, governance, tombstones, and audit

**Files:**
- Modify: `src/trueheart_core/ports.py`
- Modify: `src/trueheart_core/sqlite.py`
- Modify: `src/trueheart_core/service.py`
- Create: `tests/test_retention.py`
- Create: `tests/test_governance.py`
- Create: `tests/test_audit_and_atomicity.py`

**Interfaces:**
- Consumes: scope, lineage, receipts, and memories from Tasks 1-3.
- Produces: raw-body expiry, all four governance actions, non-resurrection, body-free audit, and transaction rollback evidence.

- [ ] **Step 1: Write retention RED tests**

Test that expiry before the exact timestamp changes nothing; expiry at the exact
timestamp removes only `raw_event_content`; repeated expiry is idempotent;
receipt/hash/lineage/derived memory remain; and a naive `as_of` is rejected.

- [ ] **Step 2: Implement raw-body expiry**

`expire_raw_content(as_of=...)` deletes eligible body rows in one transaction,
marks receipts expired, writes one body-free audit row per expired event, and
returns the number newly expired. It never deletes a derived memory.

- [ ] **Step 3: Write governance RED tests**

Add one focused test for each transition and rejection:

- active memory -> seal -> no recall;
- sealed memory -> restore -> recall;
- restore active, forgotten, or deleted -> `InvalidTransition` or
  `EntityDeleted` as applicable;
- forget memory removes body and edges, tombstones ID and fingerprint, preserves
  sources, and blocks rematerialization;
- delete memory has irreversible removal with `delete` audit action;
- delete raw event removes receipt/body, cascades every dependent memory, and
  tombstones all affected IDs/fingerprints;
- wrong scope returns `ScopeMismatch` without body disclosure;
- missing ID returns `EntityNotFound`;
- blank reason is rejected.

- [ ] **Step 4: Implement governance transactions**

Only memory targets support `SEAL`, `RESTORE`, and `FORGET`. Both entity types
support `DELETE`. Load scope before content projection, apply the transition,
insert tombstones and one command-level audit record, and commit atomically.
`GovernanceResult.affected_ids` is a sorted tuple containing the target and any
cascaded memory IDs.

- [ ] **Step 5: Prove body-free audit and rollback**

`tests/test_audit_and_atomicity.py` must:

1. Use unique sentinel text in event and memory bodies, perform every action,
   fetch public audit records, and inspect the `audit_log` schema/rows through a
   separate SQLite connection. Assert the sentinel never appears in a column
   name or audit value.
2. Install a temporary SQLite trigger that executes `RAISE(ABORT,
   'synthetic tombstone failure')` before a cascade tombstone insert. Call raw
   event deletion, expect a repository error, drop the trigger, and prove the
   event, body, dependent memory, and edges all remain. This exercises real
   transaction rollback without a production test hook.

- [ ] **Step 6: Verify the complete behavioral suite**

```powershell
python -m pytest tests -q
python -m ruff check src/trueheart_core tests
python -m ruff format --check src/trueheart_core tests
python -m mypy src/trueheart_core
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit lifecycle governance**

```powershell
git add src/trueheart_core tests/test_retention.py tests/test_governance.py tests/test_audit_and_atomicity.py
git commit -m "feat: enforce memory lifecycle governance"
```

### Task 5: Public documentation, example, community health, and CI

**Files:**
- Create: `README.md`
- Create: `CHANGELOG.md`
- Create: `SECURITY.md`
- Create: `CONTRIBUTING.md`
- Create: `CODE_OF_CONDUCT.md`
- Create: `SUPPORT.md`
- Create: `docs/architecture.md`
- Create: `docs/security-guarantees.md`
- Create: `docs/threat-model.md`
- Create: `examples/basic_memory.py`
- Create: `tests/test_public_api_and_example.py`
- Create: `.github/CODEOWNERS`
- Create: `.github/PULL_REQUEST_TEMPLATE.md`
- Create: `.github/ISSUE_TEMPLATE/bug_report.yml`
- Create: `.github/ISSUE_TEMPLATE/feature_request.yml`
- Create: `.github/ISSUE_TEMPLATE/config.yml`
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/codeql.yml`
- Create: `.github/workflows/dependency-review.yml`

**Interfaces:**
- Consumes: the final v0.1 public API and verified commands.
- Produces: an installable, understandable, contribution-ready public repository.

- [ ] **Step 1: Write the executable public API and example test RED**

`tests/test_public_api_and_example.py` imports every supported symbol from
`trueheart_core`, executes `examples/basic_memory.py` in a temporary working
directory, and asserts stdout is exactly:

```text
1 governed memory recalled at clarity 1.00
```

Run it before adding the example or completing public exports. Expected RED:
missing example or missing export.

- [ ] **Step 2: Add the example and explicit public exports**

The example creates a temporary SQLite database, ingests one synthetic event,
materializes one synthetic memory, recalls it, prints the exact line, and
deletes the temporary directory. It makes no network request and reads no
environment variable. `__all__` lists only stable DTOs, errors,
`SQLiteRepository`, and `TrueHeart`.

- [ ] **Step 3: Write honest public documentation**

README sections must be: purpose, guarantees, five-minute example, lifecycle,
installation, security boundaries, architecture, development, contributing,
and status. It must say `0.1.0`, local plaintext SQLite, no model/network, and
the precise non-resurrection boundary from the design.

`docs/threat-model.md` covers untrusted bodies/metadata/IDs/contributions,
scope confusion, trust escalation, memory poisoning, SQL/JSON corruption,
lineage failure, resurrection, host/provider leakage, backups, and supply chain.
For every threat, state control, residual risk, and owner.

`SECURITY.md` directs sensitive reports to GitHub private vulnerability
reporting and prohibits posting tokens, prompts, conversations, databases, or
logs in public issues. `CONTRIBUTING.md` requires synthetic fixtures, RED/GREEN
evidence, scope/security review for lifecycle changes, and signed-off provenance
for contributions. Community templates repeat the no-sensitive-data warning.

- [ ] **Step 4: Add pinned workflows**

Resolve the current immutable commit SHA for official `actions/checkout` and
`actions/setup-python` immediately before writing workflows. Use the full SHA
with an inline version comment. CI runs pytest, Ruff, mypy, package build, and
wheel installation on Python 3.11, 3.12, and 3.13. CodeQL scans Python on push,
pull request, and a weekly schedule. Dependency review runs on pull requests.
No workflow receives write permission except `security-events: write` for
CodeQL; default permissions are `contents: read`.

- [ ] **Step 5: Verify documentation and automation locally**

```powershell
python -m pytest tests -q
python -m ruff check src/trueheart_core tests examples
python -m ruff format --check src/trueheart_core tests examples
python -m mypy src/trueheart_core
python -m build
$wheelPath = (Get-ChildItem dist\trueheart_core-0.1.0-py3-none-any.whl).FullName
python -m pip install --force-reinstall $wheelPath
python examples/basic_memory.py
git diff --check
```

Expected: tests and checks exit 0, build emits sdist and wheel, wheel installs,
and the example prints the single documented line.

- [ ] **Step 6: Scan release contents and commit**

```powershell
git grep -n -I -E 'NightForest|ayla|nyra|lyra|霜月|绯夜|晓汐|CHAT_API_KEY|BEGIN (RSA|OPENSSH|EC) PRIVATE KEY'
git status --short
git add README.md CHANGELOG.md SECURITY.md CONTRIBUTING.md CODE_OF_CONDUCT.md SUPPORT.md docs examples tests/test_public_api_and_example.py .github src/trueheart_core/__init__.py
git commit -m "docs: prepare TrueHeart Core for open source"
```

Expected: the provenance/secret grep has no matches outside the design and plan
statements that explicitly prohibit copied NightForest content. Review those
two expected documentation matches manually; any production, test, fixture, or
example match blocks publication.

### Task 6: Final quality and security gate

**Files:**
- Modify only files required by findings from the final bounded review.

**Interfaces:**
- Consumes: the complete v0.1 branch.
- Produces: a verified publication candidate; it does not create a tag or release.

- [ ] **Step 1: Run the complete fresh gate**

From a clean virtual environment, install `.[dev]`, then run:

```powershell
python -m pytest tests -q
python -m ruff check src/trueheart_core tests examples
python -m ruff format --check src/trueheart_core tests examples
python -m mypy src/trueheart_core
python -m build
python examples/basic_memory.py
git diff --check
```

Capture exact counts and exit codes. Do not claim Python 3.11/3.12 compatibility
until GitHub Actions has executed those matrix jobs.

- [ ] **Step 2: Perform a bounded security review**

Review exact-scope lookups, body-bearing error paths, trust comparisons,
idempotency canonicalization, SQL parameterization, JSON checks, transaction
rollback, tombstone conflicts, raw expiry, audit columns, workflow permissions,
and package contents. Classify findings as critical, important, or minor; fix
critical/important findings in one bounded wave and rerun their covering tests.

- [ ] **Step 3: Verify the final diff and history**

```powershell
git status --short --branch
git log --oneline --decorate --show-signature
git ls-files
git diff main...HEAD --check
python -m build
$wheelPath = (Get-ChildItem dist\trueheart_core-0.1.0-py3-none-any.whl).FullName
python -m zipfile -l $wheelPath
```

Verify that package archives contain only intended source, metadata, license,
and documentation. Confirm no database, cache, secret, NightForest asset, or
local tool directory is tracked.

- [ ] **Step 4: Commit a single final repair if required**

If the bounded review required changes:

```powershell
git status --short
git diff --check
git add -u
git diff --cached --check
git commit -m "fix: close v0.1 review findings"
```

If no changes are required, create no empty commit. Publishing to GitHub,
changing visibility, pushing, tagging `v0.1.0`, creating a GitHub Release, and
publishing to PyPI are separate post-plan operations.
