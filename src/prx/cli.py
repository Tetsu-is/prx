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
    build_codex_command,
    build_proxy_environment,
    remove_owned_runtime_files,
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
from prx.settings import DEFAULT_COPILOT_MODEL, cache_directory, state_directory

app = typer.Typer(
    name="prx",
    help="Run Codex CLI through a local LiteLLM GitHub Copilot proxy.",
    no_args_is_help=True,
)


@app.command()
def doctor() -> None:
    """Check local prerequisites without sending a model request."""
    checks = collect_checks()
    for check in checks:
        marker = "OK" if check.ok else "WARN"
        typer.echo(f"[{marker}] {check.name}: {check.detail}")
    required = {"Codex CLI", "LiteLLM"}
    if any(not check.ok and check.name in required for check in checks):
        raise typer.Exit(1)
    typer.echo(
        "\nNetwork, entitlement, Responses compatibility, and billing are not tested. "
        "Run 'prx auth' and then an explicit smoke test."
    )


@app.command("version")
def version_command() -> None:
    """Print component versions."""
    typer.echo(f"prx {__version__}")
    for label, name, override in (
        ("codex", "codex", "PRX_CODEX_BIN"),
        ("copilot", "copilot", "PRX_COPILOT_BIN"),
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
    try:
        configured = load_models()
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    default_model = next(iter(configured))
    try:
        settings = create_runtime_settings(default_model, configured, port=port)
    except OSError as exc:
        typer.echo(f"Unable to reserve proxy port {port}: {exc}", err=True)
        raise typer.Exit(1) from exc
    try:
        with running_proxy(settings, show_logs=verbose_proxy) as running:
            typer.echo(f"Proxy listening at {settings.base_url}/v1")
            typer.echo(f"API key: {settings.proxy_key}")
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
        str,
        typer.Option("--copilot-model", help="Copilot model used for the authentication probe."),
    ] = DEFAULT_COPILOT_MODEL,
    verbose_proxy: Annotated[
        bool,
        typer.Option("--verbose-proxy", help="Show redacted LiteLLM logs, including device flow."),
    ] = True,
) -> None:
    """Run a minimal request that triggers GitHub's OAuth device flow.

    The probe may consume Copilot usage. It is never run by doctor.
    """
    typer.echo("Starting OAuth probe. This may consume one Copilot request.")
    settings = create_runtime_settings(copilot_model)
    try:
        try:
            with running_proxy(settings, show_logs=verbose_proxy):
                response = httpx.post(
                    f"{settings.base_url}/v1/responses",
                headers={"Authorization": f"Bearer {settings.proxy_key}"},
                json={
                    "model": settings.model,
                        "input": "Reply with exactly: PRX_AUTH_OK",
                        "max_output_tokens": 32,
                    },
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
        os.kill(pid, 0)
        if not proxy_key.startswith("sk-prx-"):
            raise ValueError("invalid proxy key")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise typer.BadParameter("No running prx proxy was found") from exc
    if shell == "fish":
        typer.echo(f"set -gx PRX_PROXY_KEY {shlex.quote(proxy_key)}")
    else:
        typer.echo(f"export PRX_PROXY_KEY={shlex.quote(proxy_key)}")
    typer.echo(f"# config.toml: base_url = \"http://127.0.0.1:{port}/v1\"")


def _remove_runtime_directory(path: Path) -> None:
    marker = path / ".prx-runtime"
    if not marker.is_file():
        return
    shutil.rmtree(path)


if __name__ == "__main__":
    app()
