from __future__ import annotations

import json
from pathlib import Path

import pytest

from prx.models import CopilotModel, load_models


def test_load_models_reads_aliases(monkeypatch, tmp_path) -> None:
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"models": {"fast": "gpt-test"}}), encoding="utf-8")
    monkeypatch.setenv("PRX_MODELS_FILE", str(path))

    assert load_models() == {"fast": CopilotModel("gpt-test", "responses")}


def test_load_models_reads_object_entries_with_api(monkeypatch, tmp_path) -> None:
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps({"models": {
            "claude": {"model": "claude-test", "api": "chat"},
            "gpt": {"model": "gpt-test"},
        }}),
        encoding="utf-8",
    )
    monkeypatch.setenv("PRX_MODELS_FILE", str(path))

    assert load_models() == {
        "claude": CopilotModel("claude-test", "chat"),
        "gpt": CopilotModel("gpt-test", "responses"),
    }


def test_load_models_rejects_unknown_api(monkeypatch, tmp_path) -> None:
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps({"models": {"x": {"model": "x", "api": "completions"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("PRX_MODELS_FILE", str(path))

    with pytest.raises(ValueError, match="unsupported api"):
        load_models()


def test_load_models_requires_non_empty_models(monkeypatch, tmp_path) -> None:
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"models": {}}), encoding="utf-8")
    monkeypatch.setenv("PRX_MODELS_FILE", str(path))

    with pytest.raises(ValueError, match="non-empty"):
        load_models()


def test_default_models_include_copilot_grok_claude_and_kimi(monkeypatch) -> None:
    monkeypatch.setenv("PRX_MODELS_FILE", str(Path(__file__).parents[1] / "models.json"))

    models = load_models()

    assert models["grok-4.7"] == CopilotModel("grok-4.7", "chat")
    assert models["claude-opus-5"] == CopilotModel("claude-opus-5", "chat")
    assert models["kimi-k3"] == CopilotModel("kimi-k3", "chat")
