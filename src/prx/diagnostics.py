from __future__ import annotations

import importlib.metadata
import os
import platform
import subprocess
from dataclasses import dataclass

from prx.runtime import resolve_binary
from prx.settings import copilot_token_directory


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def command_version(binary: str, *args: str) -> str:
    try:
        result = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    output = (result.stdout or result.stderr).strip().splitlines()
    return output[0] if output else f"exit {result.returncode}"


def collect_checks() -> list[Check]:
    checks: list[Check] = []
    codex = resolve_binary("codex", "PRX_CODEX_BIN")
    checks.append(
        Check(
            "Codex CLI",
            codex is not None,
            command_version(codex, "--version") if codex else "missing",
        )
    )
    copilot = resolve_binary("copilot", "PRX_COPILOT_BIN")
    checks.append(
        Check(
            "Copilot CLI",
            copilot is not None,
            command_version(copilot, "--version") if copilot else "missing (optional)",
        )
    )
    claude = resolve_binary("claude", "PRX_CLAUDE_BIN")
    checks.append(
        Check(
            "Claude Code",
            claude is not None,
            command_version(claude, "--version") if claude else "missing (optional)",
        )
    )
    try:
        version = importlib.metadata.version("litellm")
        checks.append(Check("LiteLLM", True, version))
    except importlib.metadata.PackageNotFoundError:
        checks.append(Check("LiteLLM", False, "not installed"))
    checks.append(Check("Python", True, platform.python_version()))
    token_dir = copilot_token_directory()
    token_files = [path for path in token_dir.iterdir() if path.is_file()]
    checks.append(
        Check(
            "Copilot OAuth cache",
            bool(token_files),
            f"{len(token_files)} file(s)" if token_files else "not authenticated through prx yet",
        )
    )
    checks.append(
        Check(
            "Loopback policy",
            os.environ.get("PRX_ALLOW_NON_LOOPBACK") is None,
            "127.0.0.1 only",
        )
    )
    return checks
