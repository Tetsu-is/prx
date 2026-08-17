from __future__ import annotations

import os
import secrets
import socket
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from platformdirs import user_cache_path, user_state_path

DEFAULT_COPILOT_MODEL = "gpt-5.6-luna"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5"
DEFAULT_CLAUDE_PLAN_MODEL = "claude-opus-5"
DEFAULT_CLAUDE_CODE_MODEL = "opusplan"
PROVIDER_ID = "prx"
Client = Literal["codex", "claude"]


def reserve_loopback_port(port: int | None = None) -> tuple[socket.socket, int]:
    """Reserve an ephemeral loopback port until the caller is ready to launch."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port or 0))
    return sock, int(sock.getsockname()[1])


def make_proxy_key() -> str:
    return f"sk-prx-{secrets.token_urlsafe(32)}"


def ensure_private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(stat.S_IRWXU)
    return path


def state_directory() -> Path:
    override = os.environ.get("PRX_STATE_DIR")
    return ensure_private_directory(
        Path(override).expanduser() if override else user_state_path("prx")
    )


def cache_directory() -> Path:
    override = os.environ.get("PRX_CACHE_DIR")
    return ensure_private_directory(
        Path(override).expanduser() if override else user_cache_path("prx")
    )


def copilot_token_directory() -> Path:
    return ensure_private_directory(state_directory() / "github-copilot")


@dataclass(frozen=True)
class RuntimeSettings:
    model: str
    port: int
    proxy_key: str
    token_directory: Path
    config_path: Path
    log_path: Path
    models: dict[str, str] = field(default_factory=dict)
    runtime_info_path: Path | None = None
    client: Client = "codex"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"
