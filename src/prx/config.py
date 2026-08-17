from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from prx.settings import (
    DEFAULT_CLAUDE_1M_MODEL,
    DEFAULT_CLAUDE_CODE_MODEL,
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_CLAUDE_PLAN_1M_MODEL,
    DEFAULT_CLAUDE_PLAN_MODEL,
    PROVIDER_ID,
    RuntimeSettings,
)


def claude_model_aliases(model: str) -> dict[str, str]:
    return {
        model: model,
        DEFAULT_CLAUDE_MODEL: DEFAULT_CLAUDE_MODEL,
        DEFAULT_CLAUDE_PLAN_MODEL: DEFAULT_CLAUDE_PLAN_MODEL,
        DEFAULT_CLAUDE_1M_MODEL: DEFAULT_CLAUDE_MODEL,
        DEFAULT_CLAUDE_PLAN_1M_MODEL: DEFAULT_CLAUDE_PLAN_MODEL,
    }


def build_litellm_config(settings: RuntimeSettings) -> dict[str, Any]:
    models = settings.models or {settings.model: settings.model}
    mode = "responses" if settings.client == "codex" else "chat"
    return {
        "model_list": [
            {
                "model_name": alias,
                "model_info": {"mode": mode},
                "litellm_params": {"model": f"github_copilot/{provider_model}"},
            }
            for alias, provider_model in models.items()
        ],
        "general_settings": {
            "master_key": f"os.environ/{proxy_key_environment_name()}",
        },
        "litellm_settings": {
            "set_verbose": False,
            "json_logs": True,
        },
    }


def write_litellm_config(settings: RuntimeSettings) -> None:
    # JSON is valid YAML and avoids adding another parser just to generate this file.
    payload = json.dumps(build_litellm_config(settings), indent=2)
    settings.config_path.write_text(payload + "\n", encoding="utf-8")
    settings.config_path.chmod(0o600)


def proxy_key_environment_name() -> str:
    return "PRX_PROXY_KEY"


def build_proxy_environment(settings: RuntimeSettings) -> dict[str, str]:
    env = os.environ.copy()
    env[proxy_key_environment_name()] = settings.proxy_key
    env["GITHUB_COPILOT_TOKEN_DIR"] = str(settings.token_directory)
    env.setdefault("LITELLM_LOG", "ERROR")
    return env


def build_claude_environment(settings: RuntimeSettings) -> dict[str, str]:
    env = build_proxy_environment(settings)
    env.pop("ANTHROPIC_API_KEY", None)
    env["ANTHROPIC_BASE_URL"] = settings.base_url
    env["ANTHROPIC_AUTH_TOKEN"] = settings.proxy_key
    env["ANTHROPIC_MODEL"] = DEFAULT_CLAUDE_CODE_MODEL
    env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = DEFAULT_CLAUDE_PLAN_1M_MODEL
    env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = DEFAULT_CLAUDE_1M_MODEL
    return env


def toml_string(value: str) -> str:
    # JSON strings and TOML basic strings share the escaping needed here.
    return json.dumps(value)


def build_codex_command(
    codex_binary: str,
    settings: RuntimeSettings,
    forwarded_args: list[str],
) -> list[str]:
    validate_forwarded_args(forwarded_args)
    base_url = f"{settings.base_url}/v1"
    overrides = [
        f"model_provider={toml_string(PROVIDER_ID)}",
        f"model={toml_string(settings.model)}",
        f"model_providers.{PROVIDER_ID}.name={toml_string('prx GitHub Copilot')}",
        f"model_providers.{PROVIDER_ID}.base_url={toml_string(base_url)}",
        f"model_providers.{PROVIDER_ID}.env_key={toml_string(proxy_key_environment_name())}",
        f"model_providers.{PROVIDER_ID}.wire_api={toml_string('responses')}",
        f"model_providers.{PROVIDER_ID}.stream_idle_timeout_ms=300000",
    ]
    command = [codex_binary]
    for override in overrides:
        command.extend(["-c", override])
    command.extend(forwarded_args)
    return command


def build_claude_command(
    claude_binary: str,
    forwarded_args: list[str],
) -> list[str]:
    return [claude_binary, *forwarded_args]


def validate_claude_model(model: str) -> None:
    if "claude" not in model.lower():
        raise ValueError(
            f"{model!r} is not a Claude model; use a Claude model available to your Copilot account"
        )


def validate_forwarded_args(args: list[str]) -> None:
    protected_flags = {"-m", "--model", "-p", "--profile"}
    protected_config_prefixes = (
        "model=",
        "model_provider=",
        "model_providers.",
        "openai_base_url=",
    )
    for index, arg in enumerate(args):
        if arg in protected_flags or any(arg.startswith(f"{flag}=") for flag in protected_flags):
            raise ValueError(
                f"{arg!r} would bypass the prx provider; use --copilot-model before '--' instead"
            )
        if arg in {"-c", "--config"} and index + 1 < len(args):
            value = args[index + 1]
            if value.startswith(protected_config_prefixes):
                raise ValueError(f"Codex config override {value!r} is managed by prx")
        if arg.startswith("--config="):
            value = arg.partition("=")[2]
            if value.startswith(protected_config_prefixes):
                raise ValueError(f"Codex config override {value!r} is managed by prx")


def remove_owned_runtime_files(root: Path) -> list[Path]:
    removed: list[Path] = []
    if not root.exists():
        return removed
    for path in root.glob("run-*"):
        if not path.is_dir():
            continue
        marker = path / ".prx-runtime"
        if not marker.is_file():
            continue
        if _marker_process_is_running(marker):
            continue
        for child in path.iterdir():
            if child.is_file() or child.is_symlink():
                child.unlink()
        path.rmdir()
        removed.append(path)
    return removed


def _marker_process_is_running(marker: Path) -> bool:
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        pid = int(payload["pid"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A process we cannot signal still counts as active.
        return True
    return True
