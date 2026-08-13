# Architecture

TrueHeart Core 0.1.0 is a synchronous Python library with three layers.

## Domain contracts

`trueheart_core.domain` defines immutable DTOs and enums. Constructors enforce
exact scope shape, timezone-aware dates, bounded UTF-8 bodies, bounded JSON
metadata, retention relationships, and typed trust and lifecycle values.

## Lifecycle service

`TrueHeart` is the high-level entry point. It calculates content hashes,
dependency fingerprints, lifecycle dates, and deterministic recall clarity. It
does not create memories: the host supplies each `MemoryDraft` and its source
event IDs. It also does not schedule expiry; the host calls
`expire_raw_content` with a timezone-aware `as_of` value.

Recall ordering is stable: clarity descending, creation time descending, then
memory ID ascending. This makes results reproducible for the same database and
query time without embeddings or network calls.

## Repository boundary and SQLite

An internal repository protocol separates policy from persistence. It is not a
stable public import in 0.1. `SQLiteRepository` is the supported adapter. It
uses one connection per transaction, a fixed schema, parameterized SQL, foreign
keys, and WAL mode for file-backed databases.

The schema separates raw bodies from receipts. `raw_event_content` holds the
raw plaintext body, while receipt, provenance, hash, lifecycle, and lineage
data remain after expiry. Governed memory bodies live in `memories`, and
`memory_sources` records exact dependency edges. Tombstone and audit tables
have no event or memory body columns.

## Transaction boundaries

Ingest and materialization are idempotent under their scoped identifiers.
Materialization validates all source records and writes the memory and lineage
atomically. Governance checks exact scope before returning entity data. Raw
event deletion removes dependent memories and writes all required tombstones
and audit records in one transaction; any failure rolls the transaction back.

## Host boundary

The host owns authentication, authorization, prompt construction, model and
provider calls, backup policy, key management, scheduling, and any transfer of
content outside the SQLite database. The core opens no network connection and
interprets no content as instructions.
