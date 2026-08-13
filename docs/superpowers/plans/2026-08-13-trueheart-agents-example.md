# TrueHeart OpenAI Agents Example Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a reproducible OpenAI Agents SDK host example for `trueheart-core==0.1.0` with host-owned scope and no model-controlled governance or execution tools.

**Architecture:** A small Python package separates pure TrueHeart host logic from the Agents SDK adapter. The model can call one read-only, no-argument memory tool bound to local context; scope and destructive governance remain host-only.

**Tech Stack:** Python 3.11+, `trueheart-core==0.1.0`, `openai-agents>=0.20,<0.21`, pytest, Ruff, mypy, GitHub Actions.

## Global Constraints

- Repository is exactly `qingyou0420/trueheart-openai-agents-example`, MIT, Python `>=3.11`.
- Use only synthetic content and temporary or explicitly user-selected file-backed SQLite databases.
- Never read `.env`, print credentials, persist `OPENAI_API_KEY`, or call an API during tests or `--dry-run`.
- The model cannot provide or change tenant, owner, subject, memory ID, database path, or governance action.
- No file, shell, browser, computer-use, MCP, plugin, code-execution, or arbitrary-network tool.
- Recalled bodies are bounded structured data labeled `UNTRUSTED_MEMORY_DATA`; documentation must not claim prompt-injection prevention.
- Live network use is limited to the OpenAI Agents SDK model request explicitly initiated by the user.
- Before `trueheart-core==0.1.0` exists on PyPI, local development installs this example with `--no-deps`, installs `openai-agents==0.20.0` separately, and installs TrueHeart Core from the reviewed local release worktree; CI is enabled only after the PyPI release is verified.

---

### Task 1: Build the host-owned memory boundary

**Files:**
- Create: `pyproject.toml`
- Create: `src/trueheart_agents_example/__init__.py`
- Create: `src/trueheart_agents_example/memory.py`
- Create: `tests/test_memory.py`

**Interfaces:**
- Produces: `AppContext`, `MemoryProjection`, `build_demo_context(path)`, `recall_for_agent(context)`, and `forget_demo_memory(context)`.

- [ ] **Step 0: Create the isolated repository and development environment**

Initialize `D:\TrueHeart-OpenAI-Agents-Example` on branch `main` with repository-local identity `qingyou0420 <qingyou0420@gmail.com>`. Create a virtual environment outside tracked paths. Until PyPI publication, install the new project with `--no-deps`, install `openai-agents==0.20.0`, and install `D:\TrueHeart-Core\.worktrees\v0.1-release` editable. Record exact versions and import paths; do not configure or read an API key.

- [ ] **Step 1: Write failing tests**

Require a fixed synthetic scope, no scope parameters in `recall_for_agent`, deterministic bounded projections, `UNTRUSTED_MEMORY_DATA` labels even for hostile instruction-like bodies, body-free projections after forget, and temporary test databases.

- [ ] **Step 2: Observe RED**

Run `python -m pytest tests/test_memory.py -q`; expect import or missing-behavior failures.

- [ ] **Step 3: Implement minimally**

Create immutable context/projection dataclasses. Seed one source and one memory through public TrueHeart Core APIs. Recall at a fixed UTC time and project only kind, trust, clarity, created time, and body, with at most five records and 1,000 UTF-8 bytes per body. Keep exact scope and memory ID private inside `AppContext`. Forget via a host-side `GovernanceCommand` only.

- [ ] **Step 4: Observe GREEN and commit**

Run focused tests, Ruff, and mypy. Commit exactly `feat: add host-owned TrueHeart memory boundary`.

### Task 2: Add the Agents SDK adapter and offline CLI

**Files:**
- Create: `src/trueheart_agents_example/agent.py`
- Create: `src/trueheart_agents_example/__main__.py`
- Create: `tests/test_agent.py`
- Create: `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 1 context/projection functions.
- Produces: `build_agent()`, one no-argument read-only function tool, `run_live(question, context)`, and CLI modes `--dry-run`, `--question`, and `--forget`.

- [ ] **Step 1: Write failing adapter and CLI tests**

Require tool output to use the Task 1 projection, require agent instructions to define recalled data as untrusted evidence, require the tool schema to expose no scope/ID/path/governance argument, require missing `OPENAI_API_KEY` to fail before `Runner.run`, require `--dry-run` to work with the variable unset and print deterministic JSON, and require `--forget` to be performed by the host after recall.

- [ ] **Step 2: Observe RED**

Run focused tests and confirm failures are due to missing adapter/CLI behavior.

- [ ] **Step 3: Implement minimally**

Use `Agent`, `Runner`, `RunContextWrapper`, and `function_tool` from the Agents SDK. Keep context local. Return projections from the read-only tool. Use static instructions that forbid treating recalled content as instructions and state that this mitigates but does not eliminate prompt injection. Check only for presence of `OPENAI_API_KEY`; never read it into logs or persistence. Do not configure any other tool.

- [ ] **Step 4: Observe GREEN and commit**

Run focused and full tests with `OPENAI_API_KEY` unset, Ruff, format, mypy, and offline CLI. Commit exactly `feat: demonstrate bounded Agents SDK recall`.

### Task 3: Document, automate, and publish the example

**Files:**
- Create: `README.md`
- Create: `LICENSE`
- Create: `SECURITY.md`
- Create: `CONTRIBUTING.md`
- Create: `.gitignore`
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: Tasks 1-2 and the released `trueheart-core==0.1.0` package.
- Produces: a public, installable demonstration repository with green CI.

- [ ] **Step 1: Write documentation**

Document architecture, quickstart, dry-run and live commands, exact data flow, no-tool boundary, host-owned scope/governance, API-key handling, prompt-injection residual risk, plaintext SQLite boundary, and links to TrueHeart Core, its threat model, and OpenAI Agents SDK context documentation.

- [ ] **Step 2: Add CI**

Pin official Actions to immutable SHAs, set `contents: read`, test Python 3.11-3.13 with `OPENAI_API_KEY` unset, install the exact package constraints, run tests/Ruff/format/mypy/build, install the wheel, and run `--dry-run` offline.

- [ ] **Step 3: Verify repository**

Use fresh environments, scan tracked files and history for secrets/private data, confirm no `.env`, database, log, prompt fixture, binary, or NightForest material exists, inspect wheel/sdist contents and metadata, and verify all tests make zero OpenAI API calls.

- [ ] **Step 4: Commit and publish**

Commit exactly `docs: prepare the Agents SDK example for public use`. Create the public GitHub repository, enable private vulnerability reporting, secret scanning, push protection, Dependabot security updates, topics, and Issues, push `main`, and wait for CI.
