# Security guarantees and boundaries

This document describes TrueHeart Core 0.1.1. Guarantees apply to operations
performed through the library against the SQLite database it controls.

## Enforced by the core

- Every public operation that acts on scoped data requires an exact
  caller-supplied `Scope`; mismatches fail closed before content is returned.
  `expire_raw_content` is a whole-database maintenance operation that only
  expires eligible raw bodies by `as_of`.
- A memory's declared trust cannot exceed its least-trusted source.
- Bodies and JSON metadata are validated and bounded, datetimes require an
  offset and normalize to UTC, SQL values are parameterized, and schema names
  are fixed.
- Memory/source/tombstone/audit lifecycle changes are atomic.
- Tombstones and audit records contain no event or memory bodies.
- The library invokes no model or network, loads no plugins, runs no shell or
  background worker, and interprets no content as instructions.

Within the library-controlled SQLite database, body-free tombstones prevent
recreation of forgotten or deleted scoped IDs and exact dependency
fingerprints. This is not cryptographic erasure and does not remove copied
databases, snapshots, backups, WAL or filesystem artifacts, host logs, or
provider content.

## Not provided

The default adapter bodies are plaintext local SQLite; version 0.1 has no
encryption at rest. There is no protection against a malicious local
administrator or compromised host.

Exact `Scope` is caller-supplied database isolation, not authentication or
authorization. The package is not a multi-tenant service or API auth layer.
The host must decide which principal may construct and use a scope. If an ID is
absent from the requested exact scope but exists elsewhere in the same database,
materialization and governance can disclose that one-bit existence by raising
`ScopeMismatch` rather than `EntityNotFound`. Hosts must treat identifiers as
sensitive and authorize both scope and ID use.

Because the core treats bodies and metadata as data, it does not establish
their truth and cannot prevent prompt injection. It cannot control host prompt
construction, provider egress, provider retention, logging, telemetry,
exports, backups, or copies.

Body-free means audit rows and tombstones omit event and memory bodies. IDs,
scope components, reasons, timestamps, provenance, and metadata can still be
sensitive and require host-appropriate handling.

Read paths validate persisted rows before they filter or truncate. This is
intentional: recall must detect a corrupt status or deadline even when that
row would not be returned.

- `recall` loads every memory and every `memory_sources` edge in the requested
  scope, validates each row, then filters in the library. Any one corrupt row
  makes that scope's recall unavailable.
- `expire_raw_content` scans every raw-event receipt in the database with no
  tenant filter, validates each receipt, then expires eligible bodies. Any one
  tenant's corrupt row makes whole-database expiry unavailable.
- `audit` loads every audit row in the requested scope, validates each row,
  then sorts and truncates in the library. Any one corrupt audit row makes
  that scope's audit unavailable.

These operations therefore have a residual denial-of-service surface: local
tampering or a single bad row can fail-close an entire scope or, for expiry,
the entire database. Hosts that need availability despite a bad row must
repair or isolate the database; the library does not skip corrupt rows.

The public `audit` method returns at most `limit` records (default and
maximum 100) and has no pagination token or time-range filter. Older rows
remain in SQLite but are not reachable through the public API.
`occurred_at` is caller-claimed, not the adapter write time: materialize
stores `MemoryDraft.created_at`, governance stores
`GovernanceCommand.occurred_at`, expire stores the caller-supplied `as_of`,
and ingest stores the service clock. Records with the same `occurred_at` are
ordered by `audit_id`, which is a random `uuid4`. The projection can show
that listed lifecycle actions occurred. It does not provide a reliable total
order of writes.

## Operational responsibilities

Hosts should restrict local database and backup access, decide whether they
need storage encryption, avoid placing secrets in IDs or metadata, authorize
scope construction, call expiry on an explicit schedule, and assess any body
before including it in a model prompt. Maintainers own dependency and workflow
review; contributors must use synthetic fixtures and declare provenance.

Metadata validation permits at most 64 JSON container levels and integers of at
most 4096 bits, in addition to the 16 KiB canonical serialization limit. The
SQLite adapter checks for the required `json_valid` function at connection open
and fails closed if it is unavailable.
