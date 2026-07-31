from __future__ import annotations

import json

import pytest

from prx.models import load_models


def test_load_models_reads_aliases(monkeypatch, tmp_path) -> None:
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"models": {"fast": "gpt-test"}}), encoding="utf-8")
    monkeypatch.setenv("PRX_MODELS_FILE", str(path))

    assert load_models() == {"fast": "gpt-test"}


def test_load_models_requires_non_empty_models(monkeypatch, tmp_path) -> None:
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"models": {}}), encoding="utf-8")
    monkeypatch.setenv("PRX_MODELS_FILE", str(path))

    with pytest.raises(ValueError, match="non-empty"):
        load_models()
