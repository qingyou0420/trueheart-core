# TrueHeart Core

## Purpose

TrueHeart Core 0.1.0 is a model-independent Python library for governed
long-term memory in AI agents. It gives a host application explicit contracts
for ingesting source events, materializing derived memories, deterministic
recall, retention, lineage, governance, and body-free audit records.

It supports Python 3.11 or newer, is licensed under MIT, has no runtime
dependencies outside the standard library, and invokes no model or network.

## Guarantees

- Every public operation uses an exact caller-supplied
  `Scope(tenant_id, owner_id, subject_id)` and fails closed on mismatches.
- Inputs are validated and bounded; public datetimes are timezone-aware and
  normalized to UTC.
- Trust is explicit and a derived memory cannot exceed its least-trusted source.
- Materialization, lineage, tombstones, and audit changes are transactional.
- Recall is deterministic: clarity descending, creation time descending, then
  memory ID ascending. Clarity is `1.0` through `clear_until`, then fades
  linearly to `0.0` at `recall_until`.
- Tombstones and audit records contain no event or memory bodies.
- The core performs no model calls, networking, plugin loading, shell execution,
  vector search, prompt rendering, or background work.

Within the library-controlled SQLite database, body-free tombstones prevent
recreation of forgotten or deleted scoped IDs and exact dependency
fingerprints. This is not cryptographic erasure and does not remove copied
databases, snapshots, backups, WAL or filesystem artifacts, host logs, or
provider content.

## Five-minute example

Start from a repository checkout because the runnable example is a repository
file and is not installed by the wheel:

```console
git clone https://github.com/qingyou0420/trueheart-core.git
cd trueheart-core
python -m pip install -e ".[dev]"
python examples/basic_memory.py
```

It prints exactly:

```text
1 governed memory recalled at clarity 1.00
```

The example uses `TemporaryDirectory`, creates one local file-backed SQLite
database, ingests a `RawEventDraft`, creates a caller-authored `MemoryDraft`,
and recalls it with a `RecallQuery`. It reads no environment variables, makes
no network requests, and removes the temporary database on exit. See
[`examples/basic_memory.py`](https://github.com/qingyou0420/trueheart-core/blob/main/examples/basic_memory.py)
for the full code.

The high-level API is `TrueHeart`:

- `ingest_event(RawEventDraft)` records a source receipt and its local body.
- `materialize_once(MemoryDraft)` stores caller-derived content with lineage.
- `recall(RecallQuery)` returns an immutable tuple of `RecallItem` values.
- `govern(GovernanceCommand)` seals, restores, forgets, or deletes.
- `expire_raw_content(as_of=...)` removes eligible raw bodies.
- `audit(scope, limit=...)` returns body-free lifecycle records.

`SQLiteRepository` is the supported adapter. Stable imports also include the
immutable DTOs and enums (`Scope`, `SourceRef`, `RetentionPolicy`,
`RawEventDraft`, `RawEventReceipt`, `MemoryDraft`, `MemoryRecord`,
`MemoryStatus`, `RecallQuery`, `RecallItem`, `GovernanceCommand`,
`GovernanceAction`, `GovernanceResult`, `EntityType`, `AuditRecord`, and
`TrustLevel`) and the documented `TrueHeartError` subclasses.

## Lifecycle

1. The host supplies an event, exact scope, provenance, trust, and retention.
2. The host—not this library—derives a memory and names its source event IDs.
3. Recall returns only active, unexpired memories in deterministic order.
4. The host explicitly calls raw-body expiry; no scheduler runs in the core.
5. Seal and restore are reversible. Forget and delete remove controlled bodies
   and create non-resurrection tombstones inside the current database.

Deleting a raw event also deletes and tombstones dependent memories in one
transaction. Forgetting a memory does not delete its source events.

## Installation

TrueHeart Core 0.1.0 is available from
[PyPI](https://pypi.org/project/trueheart-core/0.1.0/):

```console
python -m pip install trueheart-core==0.1.0
```

For development from a checkout:

```console
python -m pip install -e ".[dev]"
```

The default adapter stores bodies as plaintext in local SQLite. Version 0.1
provides no encryption at rest.

## Integrations

The maintainer-owned
[OpenAI Agents SDK integration example](https://github.com/qingyou0420/trueheart-openai-agents-example)
shows a host-owned, bounded memory integration and depends on `openai-agents`.
Any explicitly initiated model API calls and associated network traffic occur
in the host application, outside TrueHeart Core's dependency-free,
model-independent, and network-independent runtime boundary.

## Security boundaries

The core treats content and metadata as data and never interprets them as
instructions. It cannot prevent prompt injection, establish truth, or control
how a host constructs prompts or sends content to a provider.

`Scope` is caller-supplied database isolation, not authentication or
authorization. TrueHeart Core is not a multi-tenant service or API auth layer.
When a requested ID is absent from the exact scope but exists elsewhere in the
same database, materialization and governance can distinguish that one-bit
existence through `ScopeMismatch` rather than `EntityNotFound`. IDs are
sensitive; hosts must authenticate and authorize both scope and ID use. The
core does not protect against a malicious local administrator or compromised
host. Body-free audit and tombstone records omit event and memory bodies, but
their identifiers and metadata can still be sensitive.

Metadata is limited to 64 JSON container levels, integer magnitude of at most
4096 bits, and 16 KiB after canonical serialization. The SQLite adapter checks
for the standard `json_valid` function when it opens a connection and fails
closed if that required capability is unavailable.

Read the
[security guarantees](https://github.com/qingyou0420/trueheart-core/blob/main/docs/security-guarantees.md),
the
[threat model](https://github.com/qingyou0420/trueheart-core/blob/main/docs/threat-model.md),
and
[security reporting](https://github.com/qingyou0420/trueheart-core/blob/main/SECURITY.md)
before using the library with sensitive data.

## Architecture

Immutable domain contracts feed the `TrueHeart` lifecycle service, which uses
an internal repository boundary implemented by `SQLiteRepository`. Policy and
validation live in the domain/service layer; persistence, transactions, and
projection assembly live in the adapter. See the
[architecture](https://github.com/qingyou0420/trueheart-core/blob/main/docs/architecture.md).

## Development

```console
python -m pytest tests -q
python -m ruff check src/trueheart_core tests examples
python -m ruff format --check src/trueheart_core tests examples
python -m mypy src/trueheart_core
python -m build
```

Tests and examples use synthetic data and temporary file-backed databases.

## Contributing

Contributions are welcome under the requirements in
[CONTRIBUTING.md](https://github.com/qingyou0420/trueheart-core/blob/main/CONTRIBUTING.md).
Report ordinary usage questions as described in
[SUPPORT.md](https://github.com/qingyou0420/trueheart-core/blob/main/SUPPORT.md),
and report vulnerabilities privately as described in
[SECURITY.md](https://github.com/qingyou0420/trueheart-core/blob/main/SECURITY.md).

## Status

Version 0.1.0 is published on PyPI from this public source repository and keeps
an intentionally small API. A point-in-time
[code review of 0.1.0 (in Chinese)](https://github.com/qingyou0420/trueheart-core/blob/main/docs/reviews/2026-08-13-v0.1.0-code-review.md)
covers `main` at commit `bda22c7`; it is a snapshot, not a living guarantee. Semantic search, embeddings, model integration,
service authentication, provider deletion, backup management, and encryption
at rest are out of scope.
