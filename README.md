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
git clone <repository-url>
cd prx
uv sync
uv tool install --editable .
uv tool update-shell
prx doctor
```

This installs the project's `prx` command into the environment of each user
who clones this repository. Because `--editable` is specified, changes to the
Python code or `models.json` in the repository take effect immediately.
After `uv tool update-shell`, open a new terminal or apply the printed PATH
configuration to your current shell.

If you do not want to install the project as a global command, you can
instead run it from the project directory in the following form.

```bash
uv run prx doctor
uv run prx setup
uv run prx proxy
```

If you want to always invoke it as `prx` from the shell regardless of where
you cloned it, use `uv tool install --editable .`.

The project pins LiteLLM exactly in `pyproject.toml` and `uv.lock`. Review
dependency changes before upgrading it.

## First use

First check the model list in the official Copilot CLI using its `/model`
command. LiteLLM currently does not expose authoritative account model
discovery.

Then trigger OAuth device flow with a model available to your account:

```bash
prx auth --copilot-model gpt-5.6-luna
```

`prx auth` sends a minimal model request and may consume Copilot usage. Device
flow output is shown from redacted LiteLLM logs. OAuth credentials are stored in
the platform-specific `prx/github-copilot` state directory with user-only
directory permissions.

`prx auth` and `prx codex` use ephemeral proxy credentials and do not require
setup. `prx auth` performs OAuth and may send a model request; `prx setup` is a
local-only command that prepares credentials for a standalone proxy.

Start Codex:

```bash
prx codex --copilot-model gpt-5.6-luna -- \
  --sandbox workspace-write
```

Start only the proxy and keep it running:

```bash
prx setup
prx proxy
```

`prx setup` creates `proxy-key` in the platform-specific `prx` state directory
and prints its path. Set `PRX_STATE_DIR` to use a different state directory;
use the same setting for setup, proxy startup, and setenv.
The directory is created with mode `0700` and the key file with
mode `0600`; running setup again preserves the existing key and does not print
it. An empty or invalid key file causes setup to fail. Starting `prx proxy`
without a saved key exits with instructions to run `prx setup`.

The proxy reads model aliases from `models.json` and uses loopback port `4000`
by default. It uses the configured standalone key, which remains stable across
proxy restarts. It routes the requested `model` to the corresponding Copilot
model. Add aliases to that file before starting the proxy. Use
`prx proxy --port PORT` if you need a different fixed port.

Codex's Auto mode sends the internal model ID `codex-auto-review`. The proxy
handles that ID automatically and routes it to the Copilot model `gpt-5.6-sol`;
it does not need to be added to `models.json`.

Configure Codex to use the standalone proxy in `~/.codex/config.toml`:

```toml
model = "gpt-5.6-sol"
model_provider = "prx"

[model_providers.prx]
name = "prx GitHub Copilot"
base_url = "http://127.0.0.1:4000/v1"
env_key = "PRX_PROXY_KEY"
wire_api = "responses"
stream_idle_timeout_ms = 300000
```

The proxy is loopback-only. After the proxy is running, load its active key in
the shell where you run `codex` with `eval "$(prx proxy setenv)"` for bash/zsh
(or use one of the startup snippets below). The key is read from the running
proxy, so the `base_url` must match the fixed port selected for the proxy.

To load the API key automatically whenever a new shell starts, add the
corresponding snippet to your shell startup file.

If `prx` is installed as a uv tool, for zsh (`~/.zshrc`):

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

This reads the credentials of an already-running proxy and exports its active
`PRX_PROXY_KEY` into the current shell. It reads runtime information rather
than the saved `proxy-key` file and does not start the proxy.
Bash and zsh use `export`; fish uses `set -gx`.

To rotate the persistent standalone key:

```bash
prx setup --rotate-key
```

Rotation atomically replaces the key on disk. If a proxy is running, restart
it before using the new key, then reload the shell environment with
`eval "$(prx proxy setenv)"` for bash/zsh (or the fish snippet above). Update any
keys saved in client configurations as well. Until the proxy is restarted,
`prx proxy setenv` continues to return the old key from the running proxy.
An empty or invalid key file also causes rotation to fail.

If you do not install `prx` globally, replace `prx` in the snippets above
with the absolute path to the project virtualenv executable, for example
`/path/to/prx/.venv/bin/prx`.

Alternatively, use `prx codex`; it starts the proxy and injects the provider
configuration automatically, so no standalone-proxy settings are needed in
`config.toml`.

Everything after `--` is passed to Codex. `--model`, `--profile`, and provider
configuration overrides are rejected because they could bypass the proxy.

Useful commands:

```bash
prx doctor
prx models
prx version
prx cleanup
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
