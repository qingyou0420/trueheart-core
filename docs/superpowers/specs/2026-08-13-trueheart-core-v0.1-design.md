# TrueHeart Core v0.1 Design

Status: approved by the project owner on 2026-08-13.

## Objective

TrueHeart Core is a model-independent Python library for governed long-term
memory in AI agents. Version 0.1 focuses on the security-sensitive memory
lifecycle: explicit provenance, exact scope isolation, deterministic recall,
retention, dependency lineage, reversible sealing, irreversible forgetting and
deletion, and body-free audit records.

The implementation is new. It does not copy code, tests, prose, fixtures,
commit history, private prompts, character identities, artwork, or runtime data
from either NightForest repository.

## Approaches considered

1. A single SQLite helper would be quick, but its storage details would become
   the public API and make future adapters difficult.
2. A full event-sourced platform would provide maximum flexibility, but it
   would add infrastructure, projections, and operational complexity before the
   core contracts have users.
3. A domain service over a small repository protocol provides stable contracts
   while keeping v0.1 compact. This is the selected approach.

## Package and runtime

- Repository: `trueheart-core`.
- Distribution: `trueheart-core`.
- Import package: `trueheart_core`.
- Python: 3.11 or newer.
- Runtime dependencies: none outside the Python standard library.
- Default adapter: synchronous `sqlite3` with explicit transactions.
- License: MIT, copyright `2026 qingyou0420`.

The core never opens a network connection, invokes a model, loads plugins,
creates background workers, or renders prompts. Hosts decide whether and how
memory content is sent to an external model.

## Components

### Domain types

`domain.py` owns immutable public values and enums:

- `Scope(tenant_id, owner_id, subject_id)` is required on every public
  operation. Each component is a non-empty string of at most 128 characters.
- `TrustLevel` is ordered as `UNTRUSTED`, `OBSERVED`, and `CONFIRMED`.
- `SourceRef(source_id, source_type, occurred_at, trust, metadata)` describes
  where an event came from without claiming that its body is true.
- `RawEventDraft(event_id, scope, source, content, retention, metadata)` is the
  caller-supplied ingest contract.
- `RetentionPolicy(raw_ttl, clear_for, recall_for)` uses positive timedeltas;
  `recall_for` must not be shorter than `clear_for`.
- `MemoryDraft(memory_id, scope, content, source_event_ids, kind, trust,
  created_at, metadata)` is caller-supplied derived content. The library never
  generates summaries or facts.
- `RawEventReceipt`, `MemoryRecord`, `RecallItem`, `GovernanceResult`, and
  `AuditRecord` are read-only projections.
- `GovernanceAction` contains `SEAL`, `RESTORE`, `FORGET`, and `DELETE`.
- `EntityType` contains `RAW_EVENT` and `MEMORY`.

Metadata must be a JSON object composed only of strings, finite numbers,
booleans, null, lists, and nested string-keyed objects. Its serialized form is
limited to 16 KiB. Event and memory content is non-empty UTF-8 text limited to
256 KiB.

All datetimes must contain a UTC offset. They are normalized to UTC before
storage and returned as timezone-aware UTC values.

### Public service

`TrueHeart` is the only high-level entry point:

```python
class TrueHeart:
    def ingest_event(self, draft: RawEventDraft) -> RawEventReceipt: ...
    def materialize_once(self, draft: MemoryDraft) -> MemoryRecord: ...
    def recall(self, query: RecallQuery) -> tuple[RecallItem, ...]: ...
    def govern(self, command: GovernanceCommand) -> GovernanceResult: ...
    def expire_raw_content(self, *, as_of: datetime) -> int: ...
    def audit(self, scope: Scope, *, limit: int = 100) -> tuple[AuditRecord, ...]: ...
```

`RecallQuery` requires an exact scope and timezone-aware `as_of`, with a limit
from 1 through 100 and optional memory kinds. Recall returns only active
memories whose `recall_until` is later than `as_of`. Ordering is deterministic:
clarity descending, creation time descending, then memory ID ascending.

Clarity is `1.0` through `clear_until`, then decreases linearly to `0.0` at
`recall_until`. This calculation is pure and uses caller-supplied `as_of`.

### Repository boundary

`ports.py` defines a `Repository` protocol around atomic operations. The
service contains validation and lifecycle policy; the SQLite adapter contains
only persistence, transaction, and projection assembly logic. A future adapter
must pass the same public contract suite.

The protocol is intentionally not a generic query builder. It exposes the
minimum operations required by the six service methods.

### SQLite adapter

`SQLiteRepository` owns one database path and opens a connection per public
transaction. It enables foreign keys and WAL mode for file-backed databases.
The special `:memory:` path is rejected in v0.1 because connection-per-
transaction semantics would create isolated databases.

Schema version 1 contains:

- `schema_migrations(version, applied_at)`;
- `raw_events`, containing scope, source metadata, hashes, lifecycle dates,
  trust, status, and no plaintext body;
- `raw_event_content(event_id, content)`, the only raw-body table;
- `memories`, containing governed derived memory bodies and lifecycle dates;
- `memory_sources(memory_id, event_id)`, explicit dependency edges;
- `tombstones`, containing identifiers, scope, deletion time, reason, and
  dependency fingerprint but no body;
- `audit_log`, containing action metadata but no event or memory body.

No JSON column is accepted without application validation and SQLite
`json_valid` checks. All SQL values are parameterized. Identifiers and table
names are static.

## Lifecycle invariants

### Ingest

Ingest is idempotent by `(scope, event_id)`. Repeating an identical event
returns the existing receipt. Reusing the same ID with different content,
scope, source, or policy raises `IdempotencyConflict`. A matching tombstone
raises `EntityDeleted`; deleted identifiers are never silently reused.

The adapter stores a SHA-256 content hash in the receipt and plaintext only in
`raw_event_content`. Ingest never interprets the body as instructions or
promotes its trust level.

### Materialize once

The host supplies a memory draft. In one transaction, the service verifies
that every source exists, belongs to the exact same scope, is not deleted, and
still has a receipt. A memory cannot have a trust level greater than its
least-trusted source.

Materialization is idempotent by `(scope, memory_id)`. A tombstoned memory ID or
a tombstoned dependency fingerprint cannot be recreated. Source edges and the
memory record commit atomically.

### Retention

Raw event bodies expire at `occurred_at + raw_ttl`. `expire_raw_content()`
deletes only eligible plaintext bodies and retains body-free receipts, hashes,
source metadata, lineage edges, and derived memories. There is no scheduler;
the host invokes this deterministic maintenance operation.

Derived memories remain clear for `clear_for` and then fade until
`recall_for`. Expired memories are not recalled but remain governed records
until explicitly forgotten or deleted.

### Governance

- `SEAL` changes an active memory to sealed. Its body remains stored but it is
  excluded from recall.
- `RESTORE` changes a sealed memory back to active. It cannot restore forgotten
  or deleted content.
- `FORGET` irreversibly removes a memory body and its source edges, then writes
  a tombstone for both the memory ID and dependency fingerprint. It does not
  delete source events.
- `DELETE` on a memory has the same body-removal semantics as `FORGET`, with a
  distinct audit action.
- `DELETE` on a raw event removes its body and receipt and, in the same
  transaction, deletes and tombstones every dependent memory. It retains only
  body-free tombstones and audit metadata.

Every mutation verifies the exact scope before returning any entity data.
Missing scope, mismatched scope, corrupt lineage, or transaction failure fails
closed. Each successful mutation writes one body-free audit record in the same
transaction.

The non-resurrection guarantee applies only to the database controlled by this
library and to identifiers/dependency fingerprints represented by its
tombstones. It does not claim cryptographic erasure from copied databases,
filesystem snapshots, model providers, host logs, or unregistered backups.

## Errors

`errors.py` exposes stable exceptions derived from `TrueHeartError`:

- `ValidationError`;
- `EntityNotFound`;
- `ScopeMismatch`;
- `IdempotencyConflict`;
- `EntityDeleted`;
- `InvalidTransition`;
- `TrustEscalation`;
- `RepositoryCorruption`.

Exceptions may contain identifiers and field names but never event or memory
bodies.

## Security boundaries

Version 0.1 guarantees:

- exact, fail-closed scope checks before content is returned;
- no automatic trust promotion;
- bounded and validated inputs;
- parameterized SQL and fixed schema names;
- atomic memory/source/tombstone/audit mutations;
- body-free tombstones and audit records;
- no model, network, plugin, shell, vector extension, or native dependency;
- synthetic tests and examples only.

Version 0.1 does not claim:

- prevention of prompt injection in a host application;
- truthfulness of user, model, or imported content;
- encryption at rest or protection from a malicious local administrator;
- multi-tenant service authentication or authorization;
- secure arbitrary import/export or backup management;
- semantic search, embedding privacy, emotion modeling, medical validity, or
  provider-side deletion.

The threat model treats event bodies, derived memory bodies, metadata, IDs, and
all future contributions as untrusted.

## Tests

The contract suite uses temporary file-backed SQLite databases and real public
APIs. It contains no network mocks. Required behaviors include:

- validation of scopes, timezones, JSON metadata, size limits, and retention;
- idempotent ingest and conflict detection;
- exact scope isolation with no content in errors or audit rows;
- atomic materialization and trust-ceiling enforcement;
- deterministic clarity and recall ordering;
- raw-body expiry without loss of provenance or derived memory;
- reversible seal/restore;
- irreversible forget/delete with dependency tombstones;
- raw-event cascade deletion and rollback on injected transaction failure;
- persistence across repository instances;
- schema initialization, version rejection, and corrupt-row failure;
- public API exports and a runnable end-to-end example.

The initial quality gate is `pytest`, Ruff, mypy, package build, and installation
of the built wheel into a clean virtual environment. GitHub Actions runs on
Python 3.11, 3.12, and 3.13 using actions pinned to full commit SHAs.

## Open-source repository

The first public release includes:

- an English README with a five-minute example and precise non-goals;
- MIT `LICENSE`;
- `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, and `SUPPORT.md`;
- architecture, threat-model, and security-guarantee documentation;
- issue forms, a pull-request template, and `CODEOWNERS`;
- dependency review and CodeQL workflows in addition to the test workflow;
- a changelog, semantic version `0.1.0`, and an annotated source tag only after
  final verification and separate publication approval.

Stars are not an acceptance criterion. The repository should earn adoption by
providing a useful API, honest guarantees, real examples, responsive issue
handling, and verifiable maintenance.

## Completion criteria

The repository is ready for public review when all planned contract tests and
quality gates pass, the built wheel installs cleanly, a security review has no
unresolved high-severity findings, the git history contains no secrets or
copied NightForest content, and public documentation matches implemented
behavior. Publishing the repository and creating a release remain distinct
operations.
