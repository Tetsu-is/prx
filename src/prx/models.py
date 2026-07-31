from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Final

DEFAULT_MODELS_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "models.json"


def models_file() -> Path:
    override = os.environ.get("PRX_MODELS_FILE")
    return Path(override).expanduser() if override else DEFAULT_MODELS_PATH


def load_models() -> dict[str, str]:
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
    models: dict[str, str] = {}
    for alias, provider_model in raw_models.items():
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError(f"Model aliases in {path} must be non-empty strings")
        if not isinstance(provider_model, str) or not provider_model.strip():
            raise ValueError(f"Model {alias!r} in {path} must map to a non-empty string")
        models[alias] = provider_model
    return models
