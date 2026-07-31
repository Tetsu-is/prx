# prx

`prx` starts a loopback-only LiteLLM Proxy and launches Codex CLI with an
ephemeral custom model provider. Model requests are routed to LiteLLM's
`github_copilot` provider.

> [!WARNING]
> This is an experimental, single-user proof of concept. LiteLLM's
> `github_copilot` integration is not the official GitHub Copilot SDK: it calls
> the Copilot Chat API using editor-compatible headers. Confirm that this use is
> permitted for your account and organization before running it. Compatibility,
> billing attribution, and account safety are not guaranteed.

## Requirements

- macOS and Python 3.12+
- `uv`
- Codex CLI
- A GitHub account entitled to use GitHub Copilot

The GitHub Copilot CLI is recommended for checking account model availability,
but it is not on the model request path:

```text
Codex CLI -> 127.0.0.1 LiteLLM /v1/responses -> GitHub Copilot Chat API
```

## Install

```bash
uv sync
uv run prx doctor
```

The project pins LiteLLM exactly in `pyproject.toml` and `uv.lock`. Review
dependency changes before upgrading it.

## First use

First check the model list in the official Copilot CLI using its `/model`
command. LiteLLM currently does not expose authoritative account model
discovery.

Then trigger OAuth device flow with a model available to your account:

```bash
uv run prx auth --copilot-model gpt-5.6-luna
```

`prx auth` sends a minimal model request and may consume Copilot usage. Device
flow output is shown from redacted LiteLLM logs. OAuth credentials are stored in
the platform-specific `prx/github-copilot` state directory with user-only
directory permissions.

Start Codex:

```bash
uv run prx codex --copilot-model gpt-5.6-luna -- \
  --sandbox workspace-write
```

Everything after `--` is passed to Codex. `--model`, `--profile`, and provider
configuration overrides are rejected because they could bypass the proxy.

Useful commands:

```bash
uv run prx doctor
uv run prx models
uv run prx version
uv run prx cleanup
```

Set `PRX_CODEX_BIN`, `PRX_COPILOT_BIN`, or `PRX_LITELLM_BIN` to override binary
locations. `PRX_STATE_DIR` and `PRX_CACHE_DIR` override state and runtime
directories for testing.

## Verification gate

Do not assume that successful output proves the intended billing route. Before
regular use:

1. Record GitHub Copilot usage and OpenAI API usage.
2. Run one small prompt through `prx codex`.
3. Confirm GitHub Copilot usage increased as expected.
4. Confirm OpenAI API usage did not increase.
5. Exercise a read, file edit, and shell tool call to verify Responses API tool
   compatibility.
6. Stop if GitHub's terms, organization policy, billing attribution, or tool
   compatibility is unclear.

Logs are written only to an ephemeral runtime directory and are deleted after a
normal run. They are redacted for common credential shapes, but verbose logging
should still be treated as sensitive.

## Development

```bash
uv run ruff check .
uv run mypy
uv run pytest --cov=prx
```
