from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from prx.config import (
    build_codex_command,
    build_litellm_config,
    remove_owned_runtime_files,
    write_litellm_config,
)
from prx.settings import RuntimeSettings


def settings(tmp_path: Path) -> RuntimeSettings:
    return RuntimeSettings(
        model="gpt-test",
        port=4567,
        proxy_key="sk-prx-secret",
        token_directory=tmp_path / "tokens",
        config_path=tmp_path / "litellm.json",
        log_path=tmp_path / "proxy.log",
    )


def test_litellm_config_routes_responses_model(tmp_path) -> None:
    payload = build_litellm_config(settings(tmp_path))
    deployment = payload["model_list"][0]
    assert deployment["model_name"] == "gpt-test"
    assert deployment["model_info"]["mode"] == "responses"
    assert deployment["litellm_params"]["model"] == "github_copilot/gpt-test"
    assert payload["general_settings"]["master_key"] == "os.environ/PRX_PROXY_KEY"
    assert "sk-prx-secret" not in json.dumps(payload)


def test_written_config_is_private_json_yaml(tmp_path) -> None:
    runtime = settings(tmp_path)
    write_litellm_config(runtime)
    assert json.loads(runtime.config_path.read_text())["model_list"]
    assert runtime.config_path.stat().st_mode & 0o077 == 0


def test_codex_command_has_ephemeral_provider(tmp_path) -> None:
    command = build_codex_command("codex", settings(tmp_path), ["--sandbox", "read-only"])
    joined = "\n".join(command)
    assert command[0] == "codex"
    assert 'model_provider="prx"' in command
    assert 'model="gpt-test"' in command
    assert "http://127.0.0.1:4567/v1" in joined
    assert "env_key" in joined
    assert "sk-prx-secret" not in joined
    assert command[-2:] == ["--sandbox", "read-only"]


@pytest.mark.parametrize(
    "args",
    [
        ["--model", "openai-model"],
        ["-m=openai-model"],
        ["--profile", "other"],
        ["-c", "model_provider=\"openai\""],
        ["--config=model_providers.bad.base_url=\"https://example.com\""],
    ],
)
def test_codex_command_rejects_provider_bypass(tmp_path, args) -> None:
    with pytest.raises(ValueError):
        build_codex_command("codex", settings(tmp_path), args)


def test_cleanup_only_removes_marked_directories(tmp_path) -> None:
    owned = tmp_path / "run-owned"
    owned.mkdir()
    (owned / ".prx-runtime").write_text("owned")
    (owned / "proxy.log").write_text("log")
    unowned = tmp_path / "run-unowned"
    unowned.mkdir()
    (unowned / "keep").write_text("keep")

    removed = remove_owned_runtime_files(tmp_path)

    assert removed == [owned]
    assert not owned.exists()
    assert unowned.exists()


def test_cleanup_keeps_runtime_owned_by_live_process(tmp_path) -> None:
    active = tmp_path / "run-active"
    active.mkdir()
    (active / ".prx-runtime").write_text(json.dumps({"pid": os.getpid()}))
    (active / "proxy.log").write_text("log")

    assert remove_owned_runtime_files(tmp_path) == []
    assert active.exists()
