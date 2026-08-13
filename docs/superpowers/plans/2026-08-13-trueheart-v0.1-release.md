# TrueHeart Core v0.1 Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a verified GitHub and PyPI release of `trueheart-core==0.1.0` without long-lived package credentials.

**Architecture:** A release-published GitHub Actions workflow builds and verifies artifacts once, then sends those exact artifacts to the GitHub Release and PyPI. Documentation is corrected before tagging, and repository settings enforce a manually approved Trusted Publishing boundary.

**Tech Stack:** Python 3.11-3.13, setuptools, PyPA build, GitHub Actions, GitHub Releases, PyPI Trusted Publishing.

## Global Constraints

- Keep `trueheart-core==0.1.0`, Python `>=3.11`, MIT, and zero runtime dependencies.
- Never store a PyPI API token or any OpenAI credential.
- Use `.github/workflows/release.yml` and GitHub environment `pypi`.
- Release tag is exactly `v0.1.0`; title is exactly `TrueHeart Core 0.1.0`.
- Build one wheel and one sdist, generate `SHA256SUMS`, and publish the exact verified artifacts.
- Preserve all documented security boundaries; do not claim encryption, cryptographic erasure, prompt-injection prevention, authentication, or broad adoption.
- Action pins: checkout `3d3c42e5aac5ba805825da76410c181273ba90b1` (`v7.0.1`), setup-python `5fda3b95a4ea91299a34e894583c3862153e4b97` (`v7.0.0`), upload-artifact `043fb46d1a93c77aae656e7c1c64a875d1fc6a0a` (`v7.0.1`), download-artifact `3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c` (`v8.0.1`), PyPI publish `dc37677b2e1c63e2034f94d8a5b11f265b73ba33` (`v1.14.2`).

---

### Task 1: Correct public release documentation

**Files:**
- Modify: `README.md`
- Modify: `SECURITY.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: current public repository state and enabled private vulnerability reporting.
- Produces: accurate pre-release documentation without claiming completed PyPI or integration publication.

- [ ] **Step 1: Record the current stale statements**

Run `rg -n "will be enabled|prepared for initial public review|after release" README.md SECURITY.md` and retain the output in the task report.

- [ ] **Step 2: Update documentation minimally**

State that private vulnerability reporting is enabled. State that the source repository is public and that publishing the `v0.1.0` GitHub Release will run the package publication workflow. Keep the PyPI command explicitly conditional on successful publication. Do not link to the integration repository before it is public and green.

- [ ] **Step 3: Verify prose and links**

Run `git diff --check`, the existing documentation/provenance scans from Task 6, and a public HTTP check for every new URL. Confirm no statement claims current PyPI availability.

- [ ] **Step 4: Run the full source gate**

Run pytest, Ruff check, Ruff format check, mypy, build, clean-wheel install, and `examples/basic_memory.py` with exact output.

- [ ] **Step 5: Commit**

Commit exactly `docs: update public release status`.

### Task 2: Add the credentialless release workflow

**Files:**
- Create: `.github/workflows/release.yml`
- Create: `scripts/verify_release_artifacts.py`
- Create: `tests/test_release_artifacts.py`

**Interfaces:**
- Consumes: `pyproject.toml` version `0.1.0`, repository release event, and the immutable Action SHAs in Global Constraints.
- Produces: verified wheel, sdist, `SHA256SUMS`, GitHub Release assets, and PyPI publication through environment `pypi`.

- [ ] **Step 1: Write failing artifact-verifier tests**

Tests create controlled fake `dist/` trees and require the verifier to reject a wrong version, missing wheel/sdist, extra distribution, unsafe archive member, missing MIT metadata, runtime dependency, or wrong Python requirement; they require a success result for one safe wheel and one safe sdist. Name the production break each test catches.

- [ ] **Step 2: Run tests and observe RED**

Run `python -m pytest tests/test_release_artifacts.py -q`; expected failure is the missing verifier module or behavior.

- [ ] **Step 3: Implement the verifier**

Use only the standard library. Parse wheel `METADATA`, inspect wheel `RECORD` hashes, inspect tar members without extraction, require `Name: trueheart-core`, `Version: 0.1.0`, `Requires-Python: >=3.11`, and `License-Expression: MIT`. Reject every unguarded runtime dependency and every unknown optional dependency; permit only the four exact existing `dev` extra requirements for build, mypy, pytest, and Ruff. Require exactly one `trueheart_core-0.1.0-py3-none-any.whl` and exactly one `trueheart_core-0.1.0.tar.gz`. Generate deterministic lowercase SHA-256 lines in `SHA256SUMS` for both artifacts.

- [ ] **Step 4: Run tests and observe GREEN**

Run the focused test, then all 174 existing tests plus the new tests, Ruff, format, and mypy.

- [ ] **Step 5: Create the workflow**

Trigger only on `release: types: [published]`. Build from `github.event.release.tag_name`, reject tags other than `v0.1.0`, use Python 3.13, install `build==1.5.0`, build, invoke the verifier, create a clean venv, install the wheel with `--no-deps`, run `pip check` and the example, then upload one artifact bundle. The GitHub asset job downloads and uploads `dist/*` plus `SHA256SUMS` to the existing release. The PyPI job downloads the same bundle, uses environment `pypi`, grants only `id-token: write`, and publishes only `dist/`.

- [ ] **Step 6: Validate workflow security**

Parse all repository YAML with an available YAML parser, confirm every `uses:` is a full 40-hex SHA from Global Constraints, confirm job permissions are minimal, confirm `persist-credentials: false`, and confirm no `pull_request_target`, mutable action tag, secret name, shell interpolation of release text, or untrusted checkout exists.

- [ ] **Step 7: Commit**

Commit exactly `ci: add trusted v0.1 release workflow`.

### Task 3: Publish and verify v0.1.0

**Files:**
- Modify only if post-publication status needs correction: `README.md`

**Interfaces:**
- Consumes: merged Tasks 1-2, PyPI pending publisher, GitHub environment `pypi`, and an approved draft release.
- Produces: public GitHub Release and PyPI project with verified artifacts.

- [ ] **Step 1: Configure Trusted Publishing**

Create the PyPI pending publisher for project `trueheart-core`, owner `qingyou0420`, repository `trueheart-core`, workflow `release.yml`, environment `pypi`. Create the GitHub environment and require `qingyou0420` approval when supported.

- [ ] **Step 2: Create a draft release**

Create draft tag/release `v0.1.0` / `TrueHeart Core 0.1.0` targeting the reviewed main commit. Release notes list features, installation, verification, and the exact security limitations from the design.

- [ ] **Step 3: Publish and approve**

Publish the GitHub Release, verify the release workflow checks out the expected commit, approve only the `pypi` deployment, and wait for every job to succeed.

- [ ] **Step 4: Verify public artifacts**

Download release assets into a fresh temporary directory, run the verifier and `sha256sum` equivalent, compare the PyPI files and hashes, install `trueheart-core==0.1.0` into a clean venv, run `pip check`, import all stable exports, and run the repository example.

- [ ] **Step 5: Verify repository state**

Confirm CI and CodeQL are green, private vulnerability reporting, secret scanning, push protection, dependency alerts, and Dependabot security updates remain enabled, and no code/secret/dependency alerts are open.

### Task 4: Record verified distribution and integration evidence

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: verified PyPI project `https://pypi.org/project/trueheart-core/` and green public integration repository `https://github.com/qingyou0420/trueheart-openai-agents-example`.
- Produces: current main-branch documentation that links only to live, verified evidence; the immutable `v0.1.0` tag is not moved.

- [ ] **Step 1: Verify both public targets**

Require HTTP success, PyPI version `0.1.0`, matching artifact hashes, integration default branch `main`, and green integration CI before editing README.

- [ ] **Step 2: Update README**

State that `trueheart-core==0.1.0` is available from PyPI. Add an `Integrations` section linking to the example and state that its `openai-agents` dependency and model/network call live outside TrueHeart Core's runtime boundary.

- [ ] **Step 3: Verify and commit**

Run the full source gate and public link checks. Commit exactly `docs: link verified distribution and integration`.
