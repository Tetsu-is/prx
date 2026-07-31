from __future__ import annotations

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
from prx.runtime import (
    create_runtime_settings,
    resolve_binary,
    run_interactive_child,
    running_proxy,
)
from prx.settings import DEFAULT_COPILOT_MODEL, cache_directory

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
    """Explain model selection and show the configured default."""
    typer.echo(f"Default: {DEFAULT_COPILOT_MODEL}")
    typer.echo(
        "LiteLLM's Copilot provider does not expose authoritative account model discovery. "
        "Use '/model' in the official Copilot CLI to inspect account availability, then pass "
        "'--copilot-model MODEL' to 'prx auth' or 'prx codex'."
    )


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


def _remove_runtime_directory(path: Path) -> None:
    marker = path / ".prx-runtime"
    if not marker.is_file():
        return
    shutil.rmtree(path)


if __name__ == "__main__":
    app()
