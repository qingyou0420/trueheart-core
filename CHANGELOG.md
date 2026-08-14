# Changelog

All notable changes to TrueHeart Core are documented here.

## [0.1.1] - 2026-08-13

### Added

- PEP 561 `py.typed` marker so installed wheels and sdists expose public
  contracts to downstream type checkers.
- Public `RepositoryBusy` for transient SQLite write-lock contention
  (`SQLITE_BUSY` / `SQLITE_LOCKED`) so hosts can retry instead of treating
  lock contention as corruption.

### Changed

- Scope guarantee wording now matches the API: public operations that act on
  scoped data require exact `Scope`; `expire_raw_content` is a whole-database
  maintenance operation that only expires eligible raw bodies by `as_of`.
- Documented read-path availability: a corrupt row in a scope makes that
  scope's recall unavailable; a corrupt row in any tenant makes
  whole-database `expire_raw_content` unavailable.
- Documented that `audit` timestamps are caller-claimed, the public API
  returns only the newest `limit` records (maximum 100), and the
  projection proves occurrence rather than a reliable total order.

## [0.1.0] - 2026-08-13

### Changed

- Release documentation now reflects the public source repository, enabled
  private vulnerability reporting, and conditional package publication.

### Added

- Immutable domain contracts for exact scope, provenance, trust, retention,
  recall, governance, and body-free audit projections.
- A synchronous SQLite adapter with transactional lineage and tombstones.
- Deterministic clarity calculation and recall ordering.
- Raw-body expiry plus seal, restore, forget, and delete lifecycle operations.
- Public documentation, a runnable synthetic example, community health files,
  and SHA-pinned continuous integration and security workflows.
