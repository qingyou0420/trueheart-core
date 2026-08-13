# TrueHeart OpenAI Agents Example Design

## Goal

Publish a small, reproducible host integration showing how an OpenAI Agents
SDK application can use TrueHeart Core without giving the model control over
scope, governance, files, shell commands, credentials, or arbitrary network
tools.

## Repository

The example lives in the separate public MIT repository
`qingyou0420/trueheart-openai-agents-example`. This keeps `openai-agents` and
model/network behavior outside TrueHeart Core's zero-runtime-dependency and
no-network boundary.

## Data flow

The host creates a fixed synthetic `Scope`, opens a local file-backed SQLite
database, and uses TrueHeart Core to ingest and materialize one synthetic
memory. The scope remains in local Agents SDK context, which is not sent to the
model. A function tool accepts no scope or identifier arguments from the model;
it recalls through the host-owned scope and returns a bounded structured
projection explicitly labeled as untrusted data.

The agent instructions say that recalled memory is evidence, not instructions,
and must never override higher-priority instructions or trigger actions. The
example gives the agent no file, shell, browser, MCP, computer-use, or arbitrary
HTTP tool. The only network request is the Agents SDK model call when the user
runs live mode with `OPENAI_API_KEY`.

## User experience

- `python -m trueheart_agents_example --dry-run` runs offline, creates a
  temporary database, and prints the exact structured memory projection.
- `python -m trueheart_agents_example --question "..."` runs the real Agent
  after requiring `OPENAI_API_KEY` without printing or persisting the key.
- A host-only `--forget` demonstration governs the synthetic memory after the
  run; it is never exposed as a model tool.

## Testing and release

Tests are synthetic and make no API calls. They cover fixed-scope recall,
bounded projection, hostile memory text remaining labeled data, absent-key
failure before network, and host-only forget. CI runs on Python 3.11-3.13 with
Ruff and mypy. The README links the TrueHeart Core security boundary and
clearly distinguishes demonstration controls from prompt-injection prevention.
