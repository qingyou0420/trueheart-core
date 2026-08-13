# Contributing to TrueHeart Core

Thank you for helping improve the project. Keep changes narrow, reviewable, and
consistent with the documented security boundary.

## Before starting

- Open an issue for substantial API, lifecycle, schema, or security changes.
- Use only synthetic fixtures. Never submit real prompts, conversations,
  credentials, databases, logs, customer data, or copied private material.
- Keep runtime dependencies empty unless the project owner explicitly approves
  a design change.

## Development workflow

Install the development tools with `python -m pip install -e ".[dev]"`.

For every behavior change, follow test-driven development:

1. Add a focused test using the public behavior and observe the expected RED.
2. Make the smallest implementation change and observe GREEN.
3. Refactor only while the focused and full suites remain green.

Include the RED and GREEN commands and outputs in the pull request. Run:

```console
python -m pytest tests -q
python -m ruff check src/trueheart_core tests examples
python -m ruff format --check src/trueheart_core tests examples
python -m mypy src/trueheart_core
python -m build
```

## Security review

Changes to scope checks, trust, retention, recall, lineage, persistence,
tombstones, governance, audit records, validation, or workflows require an
explicit security review in the pull request. State the threat, control,
residual risk, and responsible boundary. Verify failures do not expose bodies.

## Provenance sign-off

Every contribution must be original or clearly identify compatible upstream
provenance and license. Add a `Signed-off-by: Name <address>` trailer to each
commit to attest that you have the right to contribute it under this
repository's MIT license. Do not include third-party material without review.

## Pull requests

- Keep fixtures and examples synthetic and deterministic.
- Update public documentation when behavior or boundaries change.
- Complete the pull request checklist and respond to review findings.
- Do not add generated build directories, wheels, source archives, databases,
  coverage files, caches, or virtual environments.
