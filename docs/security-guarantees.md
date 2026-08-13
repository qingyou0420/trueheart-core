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
