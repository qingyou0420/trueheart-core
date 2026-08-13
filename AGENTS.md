# TrueHeart Core contributor instructions

## Global constraints

- Implement from tests without copying code, tests, prose, fixtures, history, or
  other material from either NightForest repository.
- Keep runtime dependencies empty. The core must not perform network, model,
  plugin, shell, vector, or background-worker operations.
- Require an exact `Scope(tenant_id, owner_id, subject_id)` for every public
  operation that acts on scoped data and fail closed on mismatches.
  `expire_raw_content` is a whole-database maintenance operation that only
  expires eligible raw bodies by `as_of`.
- Require timezone-aware public datetimes and normalize them to UTC.
- Accept only JSON metadata of at most 16 KiB and non-empty UTF-8 content of at
  most 256 KiB.
- Keep tombstones and audit records body-free. Forget and delete must retain
  tombstones that prevent identifier or dependency resurrection.
- Use only synthetic test data. Never access NightForest files, data, prompts,
  characters, databases, or configuration.
- Do not add secrets, credentials, private prompts, or user data to the
  repository.

## Development workflow

- Follow test-driven development: observe a failing test before each production
  behavior, implement the smallest change, and run fresh verification.
- Run the relevant pytest, Ruff, mypy, and diff checks before committing.
- Pushes, tags, PyPI publication, and GitHub Releases each require separate
  explicit authorization.
