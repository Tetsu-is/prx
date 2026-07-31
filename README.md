# prx

`prx` starts a loopback-only LiteLLM Proxy. It can either run as a standalone
server or launch Codex CLI with an ephemeral custom model provider. Model
requests are routed to LiteLLM's `github_copilot` provider.

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

Start only the proxy and keep it running:

```bash
uv run prx proxy
```

The proxy reads model aliases from `models.json`, prints its local URL and
ephemeral API key, and prints a copy-pasteable `export PRX_PROXY_KEY=...`
command. Paste that command into the shell where Codex runs. It routes the
requested `model` to the corresponding Copilot model. Add aliases to that file
before starting the proxy.

Configure Codex to use the standalone proxy in `~/.codex/config.toml`:

```toml
model = "gpt-5.6-sol"
model_provider = "prx"

[model_providers.prx]
name = "prx GitHub Copilot"
base_url = "http://127.0.0.1:PORT/v1"
env_key = "PRX_PROXY_KEY"
wire_api = "responses"
stream_idle_timeout_ms = 300000
```

Replace `PORT` with the port printed by `prx proxy`, then paste the printed
`export PRX_PROXY_KEY=...` command into the shell where you run `codex`.
Because the standalone proxy currently chooses a new port and API key on each
start, repeat these two updates after restarting it. The API key is ephemeral
and the proxy is loopback-only.

To load the API key automatically whenever a new shell starts, add the
corresponding snippet to your shell startup file.

For zsh (`~/.zshrc`):

```sh
if command -v prx >/dev/null 2>&1; then
  eval "$(prx proxy setenv 2>/dev/null)"
fi
```

For bash (`~/.bashrc`):

```bash
if command -v prx >/dev/null 2>&1; then
  eval "$(prx proxy setenv --shell bash 2>/dev/null)"
fi
```

For fish (`~/.config/fish/config.fish`):

```fish
if type -q prx
  eval (prx proxy setenv --shell fish 2>/dev/null)
end
```

This reads the credentials of an already-running proxy and exports
`PRX_PROXY_KEY` into the current shell. It does not start the proxy. The
`base_url` still needs to match the port printed at startup. Bash and zsh use
`export`; fish uses `set -gx`.

Alternatively, use `prx codex`; it starts the proxy and injects the provider
configuration automatically, so no standalone-proxy settings are needed in
`config.toml`.

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
