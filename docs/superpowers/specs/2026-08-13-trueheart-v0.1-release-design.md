# TrueHeart Core v0.1 Release Design

## Goal

Publish a verifiable `v0.1.0` source and Python package release without adding
runtime dependencies or long-lived publishing credentials.

## Release boundary

- `trueheart-core==0.1.0` remains Python 3.11+, MIT, and dependency-free at
  runtime.
- A GitHub Release named `TrueHeart Core 0.1.0` owns tag `v0.1.0`.
- The release publishes one universal wheel, one source distribution, and a
  `SHA256SUMS` file.
- PyPI publication uses Trusted Publishing from `.github/workflows/release.yml`
  and the protected GitHub environment `pypi`; no PyPI token is stored.
- The release workflow starts only when a GitHub Release is published.

## Workflow

The build job checks out the released tag, builds once, validates metadata and
archive contents, installs the wheel into a clean virtual environment, and
runs `examples/basic_memory.py`. It then uploads the exact verified artifacts.
Separate jobs download that artifact: one uploads assets to the existing
GitHub Release, and one publishes the distributions to PyPI with OIDC.

The expected example output is exactly:

```text
1 governed memory recalled at clarity 1.00
```

## Security controls

- All reusable Actions use immutable commit SHAs with release comments.
- Only the PyPI job receives `id-token: write`; only the GitHub asset job
  receives `contents: write`.
- Checkout does not persist Git credentials.
- PyPI publication requires the `pypi` environment and a manual maintainer
  approval when GitHub supports that protection for this repository.
- Release notes repeat the plaintext SQLite, host-auth, prompt-injection, and
  non-cryptographic-erasure boundaries.

## Documentation

`SECURITY.md` states that private vulnerability reporting is live. Before the
tag, `README.md` states that the source repository is public and that the
`v0.1.0` workflow will publish the package. After PyPI and the separate OpenAI
Agents SDK example are public, a follow-up main-branch documentation commit
records both verified URLs without moving the release tag.

## Verification

Before publishing, the branch must pass the existing 174-test suite, Ruff,
mypy, build, clean-wheel install, example output, archive inspection, action
pin validation, and secret/provenance scans. After publication, verify the
GitHub Release assets and hashes, PyPI metadata, clean `pip install`, CI,
CodeQL, and repository security settings.
