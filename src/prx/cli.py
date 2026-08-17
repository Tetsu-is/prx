from __future__ import annotations

import json
import os
import shlex
import shutil
from pathlib import Path
from typing import Annotated

import httpx
import typer

from prx import __version__
from prx.config import (
    build_claude_command,
    build_claude_environment,
    build_codex_command,
    build_proxy_environment,
    remove_owned_runtime_files,
    validate_claude_model,
    validate_forwarded_args,
)
from prx.diagnostics import collect_checks, command_version
from prx.models import load_models, models_file
from prx.runtime import (
    create_runtime_settings,
    resolve_binary,
    run_interactive_child,
    running_proxy,
)
from prx.settings import (
    DEFAULT_CLAUDE_CODE_MODEL,
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_CLAUDE_PLAN_MODEL,
    DEFAULT_COPILOT_MODEL,
    Client,
    cache_directory,
    state_directory,
)

app = typer.Typer(
    name="prx",
    help="Run Codex CLI or Claude Code through a local LiteLLM GitHub Copilot proxy.",
    no_args_is_help=True,
)


@app.command()
def doctor(
    client: Annotated[
        str,
        typer.Option("--client", help="CLI prerequisite to check: codex or claude."),
    ] = "codex",
) -> None:
    """Check local prerequisites without sending a model request."""
    selected_client = _validate_client(client)
    checks = collect_checks()
    for check in checks:
        marker = "OK" if check.ok else "WARN"
        typer.echo(f"[{marker}] {check.name}: {check.detail}")
    required = {"LiteLLM", "Codex CLI" if selected_client == "codex" else "Claude Code"}
    if any(not check.ok and check.name in required for check in checks):
        raise typer.Exit(1)
    typer.echo(
        "\nNetwork, entitlement, API compatibility, and billing are not tested. "
        "Run 'prx auth' and then an explicit smoke test."
    )


@app.command("version")
def version_command() -> None:
    """Print component versions."""
    typer.echo(f"prx {__version__}")
    for label, name, override in (
        ("codex", "codex", "PRX_CODEX_BIN"),
        ("copilot", "copilot", "PRX_COPILOT_BIN"),
        ("claude", "claude", "PRX_CLAUDE_BIN"),
    ):
        binary = resolve_binary(name, override)
        typer.echo(f"{label}: {command_version(binary, '--version') if binary else 'not found'}")


@app.command()
def models() -> None:
    """List model aliases configured for the standalone proxy."""
    try:
        configured = load_models()
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    for alias, provider_model in configured.items():
        typer.echo(f"{alias} -> github_copilot/{provider_model}")
    typer.echo(
        f"\nConfigured in {models_file()}. LiteLLM's Copilot provider does not expose "
        "authoritative "
        "account model discovery; verify availability with the official "
        "Copilot CLI."
    )


@app.command()
def proxy(
    action: Annotated[
        str,
        typer.Argument(help="Use 'setenv' to print exports for a running proxy."),
    ] = "start",
    verbose_proxy: Annotated[
        bool,
        typer.Option("--verbose-proxy", help="Mirror redacted LiteLLM logs to stderr."),
    ] = False,
    shell: Annotated[
        str,
        typer.Option("--shell", help="Shell syntax for 'setenv': bash, zsh, or fish."),
    ] = "bash",
    client: Annotated[
        str,
        typer.Option("--client", help="Client to configure: codex or claude."),
    ] = "codex",
    copilot_model: Annotated[
        str | None,
        typer.Option("--copilot-model", help="Copilot model routed by LiteLLM."),
    ] = None,
    port: Annotated[
        int,
        typer.Option("--port", help="Loopback port for the standalone proxy."),
    ] = 4000,
) -> None:
    """Start a standalone loopback proxy until interrupted."""
    if action == "setenv":
        _print_proxy_environment(shell)
        return
    if action != "start":
        typer.echo(f"Unknown proxy action: {action}", err=True)
        raise typer.Exit(2)
    selected_client = _validate_client(client)
    try:
        configured = load_models()
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    default_model = copilot_model or (
        next(iter(configured)) if selected_client == "codex" else DEFAULT_CLAUDE_MODEL
    )
    if selected_client == "claude":
        try:
            validate_claude_model(default_model)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(2) from exc
        configured = {
            **configured,
            default_model: default_model,
            DEFAULT_CLAUDE_PLAN_MODEL: DEFAULT_CLAUDE_PLAN_MODEL,
        }
    elif default_model not in configured:
        configured = {**configured, default_model: default_model}
    try:
        settings = create_runtime_settings(
            default_model,
            configured,
            port=port,
            client=selected_client,
        )
    except OSError as exc:
        typer.echo(f"Unable to reserve proxy port {port}: {exc}", err=True)
        raise typer.Exit(1) from exc
    try:
        with running_proxy(settings, show_logs=verbose_proxy) as running:
            typer.echo(f"Proxy listening at {settings.base_url}/v1")
            typer.echo(f"API key: {settings.proxy_key}")
            if selected_client == "claude":
                typer.echo("\nCopy this into the shell where you run Claude Code:")
                typer.echo(f"export ANTHROPIC_BASE_URL={shlex.quote(settings.base_url)}")
                typer.echo(f"export ANTHROPIC_AUTH_TOKEN={shlex.quote(settings.proxy_key)}")
                typer.echo("export ANTHROPIC_MODEL=opusplan")
            else:
                typer.echo("\nCopy this into the shell where you run Codex:")
                typer.echo(f"export PRX_PROXY_KEY={shlex.quote(settings.proxy_key)}")
                typer.echo(f'# config.toml: base_url = "{settings.base_url}/v1"')
            typer.echo(f"Models: {', '.join(configured)}")
            typer.echo("Press Ctrl-C to stop.")
            try:
                running.wait()
            except KeyboardInterrupt:
                typer.echo("\nStopping proxy...")
    except RuntimeError as exc:
        typer.echo(f"Unable to start LiteLLM: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command()
def auth(
    copilot_model: Annotated[
        str | None,
        typer.Option("--copilot-model", help="Copilot model used for the authentication probe."),
    ] = None,
    client: Annotated[
        str,
        typer.Option("--client", help="API to probe: codex or claude."),
    ] = "codex",
    verbose_proxy: Annotated[
        bool,
        typer.Option("--verbose-proxy", help="Show redacted LiteLLM logs, including device flow."),
    ] = True,
) -> None:
    """Run a minimal request that triggers GitHub's OAuth device flow.

    The probe may consume Copilot usage. It is never run by doctor.
    """
    selected_client = _validate_client(client)
    selected_model = _resolve_model(selected_client, copilot_model)
    typer.echo("Starting OAuth probe. This may consume one Copilot request.")
    settings = create_runtime_settings(selected_model, client=selected_client)
    try:
        try:
            with running_proxy(settings, show_logs=verbose_proxy):
                if selected_client == "claude":
                    endpoint = f"{settings.base_url}/v1/messages"
                    payload = {
                        "model": settings.model,
                        "max_tokens": 32,
                        "messages": [
                            {
                                "role": "user",
                                "content": "Reply with exactly: PRX_AUTH_OK",
                            }
                        ],
                    }
                else:
                    endpoint = f"{settings.base_url}/v1/responses"
                    payload = {
                        "model": settings.model,
                        "input": "Reply with exactly: PRX_AUTH_OK",
                        "max_output_tokens": 32,
                    }
                response = httpx.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {settings.proxy_key}"},
                    json=payload,
                    timeout=180,
                )
                if response.is_error:
                    typer.echo(
                        f"Authentication probe failed: HTTP {response.status_code}", err=True
                    )
                    typer.echo(response.text[:1000], err=True)
                    raise typer.Exit(1)
                typer.echo("Authentication probe succeeded.")
        except RuntimeError as exc:
            typer.echo(f"Unable to start LiteLLM: {exc}", err=True)
            raise typer.Exit(1) from exc
    finally:
        _remove_runtime_directory(settings.config_path.parent)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def codex(
    ctx: typer.Context,
    copilot_model: Annotated[
        str,
        typer.Option("--copilot-model", help="GitHub Copilot model routed by LiteLLM."),
    ] = DEFAULT_COPILOT_MODEL,
    verbose_proxy: Annotated[
        bool,
        typer.Option("--verbose-proxy", help="Mirror redacted LiteLLM logs to stderr."),
    ] = False,
) -> None:
    """Start an ephemeral proxy and run Codex; pass Codex arguments after '--'."""
    codex_binary = resolve_binary("codex", "PRX_CODEX_BIN")
    if not codex_binary:
        typer.echo("Codex CLI was not found. Set PRX_CODEX_BIN or install codex.", err=True)
        raise typer.Exit(1)
    forwarded_args = list(ctx.args)
    try:
        validate_forwarded_args(forwarded_args)
    except ValueError as exc:
        typer.echo(f"Invalid Codex arguments: {exc}", err=True)
        raise typer.Exit(2) from exc
    settings = create_runtime_settings(copilot_model)
    try:
        command = build_codex_command(codex_binary, settings, forwarded_args)
        typer.echo(
            f"Starting LiteLLM on 127.0.0.1:{settings.port} "
            f"({copilot_model} -> github_copilot/{copilot_model})",
            err=True,
        )
        try:
            with running_proxy(settings, show_logs=verbose_proxy):
                env = build_proxy_environment(settings)
                exit_code = run_interactive_child(command, env)
        except RuntimeError as exc:
            typer.echo(f"Unable to start LiteLLM: {exc}", err=True)
            raise typer.Exit(1) from exc
        raise typer.Exit(exit_code)
    finally:
        _remove_runtime_directory(settings.config_path.parent)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def claude(
    ctx: typer.Context,
    copilot_model: Annotated[
        str,
        typer.Option("--copilot-model", help="GitHub Copilot Claude model routed by LiteLLM."),
    ] = DEFAULT_CLAUDE_MODEL,
    verbose_proxy: Annotated[
        bool,
        typer.Option("--verbose-proxy", help="Mirror redacted LiteLLM logs to stderr."),
    ] = False,
) -> None:
    """Start an ephemeral proxy and run Claude Code; pass Claude arguments after '--'."""
    claude_binary = resolve_binary("claude", "PRX_CLAUDE_BIN")
    if not claude_binary:
        typer.echo(
            "Claude Code was not found. Set PRX_CLAUDE_BIN or install Claude Code.",
            err=True,
        )
        raise typer.Exit(1)
    try:
        validate_claude_model(copilot_model)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    settings = create_runtime_settings(
        copilot_model,
        models={
            copilot_model: copilot_model,
            DEFAULT_CLAUDE_PLAN_MODEL: DEFAULT_CLAUDE_PLAN_MODEL,
        },
        client="claude",
    )
    forwarded_args = list(ctx.args)
    try:
        command = build_claude_command(claude_binary, forwarded_args)
        typer.echo(
            f"Starting LiteLLM on 127.0.0.1:{settings.port} "
            f"({copilot_model} -> github_copilot/{copilot_model})",
            err=True,
        )
        try:
            with running_proxy(settings, show_logs=verbose_proxy):
                env = build_claude_environment(settings)
                exit_code = run_interactive_child(command, env)
        except RuntimeError as exc:
            typer.echo(f"Unable to start LiteLLM: {exc}", err=True)
            raise typer.Exit(1) from exc
        raise typer.Exit(exit_code)
    finally:
        _remove_runtime_directory(settings.config_path.parent)


@app.command()
def cleanup() -> None:
    """Remove only stale runtime directories carrying a prx ownership marker."""
    removed = remove_owned_runtime_files(cache_directory())
    noun = "directory" if len(removed) == 1 else "directories"
    typer.echo(f"Removed {len(removed)} stale runtime {noun}.")


def _print_proxy_environment(shell: str) -> None:
    if shell not in {"bash", "zsh", "fish"}:
        raise typer.BadParameter("--shell must be bash, zsh, or fish")
    path = state_directory() / "proxy-runtime.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid = int(payload["pid"])
        port = int(payload["port"])
        proxy_key = str(payload["proxy_key"])
        client = str(payload.get("client", "codex"))
        model = str(payload.get("model", ""))
        os.kill(pid, 0)
        if not proxy_key.startswith("sk-prx-"):
            raise ValueError("invalid proxy key")
        if client not in {"codex", "claude"}:
            raise ValueError("invalid client")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise typer.BadParameter("No running prx proxy was found") from exc
    if client == "claude":
        model = DEFAULT_CLAUDE_CODE_MODEL
        if shell == "fish":
            typer.echo(f"set -gx ANTHROPIC_BASE_URL {shlex.quote(f'http://127.0.0.1:{port}')}")
            typer.echo(f"set -gx ANTHROPIC_AUTH_TOKEN {shlex.quote(proxy_key)}")
            if model:
                typer.echo(f"set -gx ANTHROPIC_MODEL {shlex.quote(model)}")
        else:
            typer.echo(f"export ANTHROPIC_BASE_URL={shlex.quote(f'http://127.0.0.1:{port}')}")
            typer.echo(f"export ANTHROPIC_AUTH_TOKEN={shlex.quote(proxy_key)}")
            if model:
                typer.echo(f"export ANTHROPIC_MODEL={shlex.quote(model)}")
    else:
        if shell == "fish":
            typer.echo(f"set -gx PRX_PROXY_KEY {shlex.quote(proxy_key)}")
        else:
            typer.echo(f"export PRX_PROXY_KEY={shlex.quote(proxy_key)}")
        typer.echo(f'# config.toml: base_url = "http://127.0.0.1:{port}/v1"')


def _validate_client(value: str) -> Client:
    if value == "codex":
        return "codex"
    if value == "claude":
        return "claude"
    raise typer.BadParameter("--client must be codex or claude")


def _resolve_model(client: Client, model: str | None) -> str:
    selected = model or (DEFAULT_CLAUDE_MODEL if client == "claude" else DEFAULT_COPILOT_MODEL)
    if client == "claude":
        try:
            validate_claude_model(selected)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    return selected


def _remove_runtime_directory(path: Path) -> None:
    marker = path / ".prx-runtime"
    if not marker.is_file():
        return
    shutil.rmtree(path)


if __name__ == "__main__":
    app()
