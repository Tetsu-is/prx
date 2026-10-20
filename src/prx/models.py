from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast, get_args

DEFAULT_MODELS_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "models.json"

CopilotApi = Literal["responses", "chat"]


@dataclass(frozen=True)
class CopilotModel:
    name: str
    # Copilot serves only some models (OpenAI's) on /responses; "chat" makes LiteLLM
    # bridge Codex's Responses API requests to /chat/completions.
    api: CopilotApi = "responses"


def models_file() -> Path:
    override = os.environ.get("PRX_MODELS_FILE")
    return Path(override).expanduser() if override else DEFAULT_MODELS_PATH


def load_models() -> dict[str, CopilotModel]:
    path = models_file()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Unable to read model file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in model file {path}: {exc}") from exc

    raw_models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(raw_models, dict) or not raw_models:
        raise ValueError(f"Model file {path} must contain a non-empty 'models' object")
    models: dict[str, CopilotModel] = {}
    for alias, entry in raw_models.items():
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError(f"Model aliases in {path} must be non-empty strings")
        models[alias] = _parse_model(alias, entry, path)
    return models


def _parse_model(alias: str, entry: object, path: Path) -> CopilotModel:
    if isinstance(entry, str):
        entry = {"model": entry}
    if not isinstance(entry, dict):
        raise ValueError(f"Model {alias!r} in {path} must be a string or an object")
    name = entry.get("model")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"Model {alias!r} in {path} must map to a non-empty string")
    api = entry.get("api", "responses")
    if api not in get_args(CopilotApi):
        raise ValueError(
            f"Model {alias!r} in {path} has unsupported api {api!r}; "
            f"use one of {', '.join(get_args(CopilotApi))}"
        )
    return CopilotModel(name, cast(CopilotApi, api))
