from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType

import httpx

from prx.config import build_proxy_environment, write_litellm_config
from prx.settings import (
    RuntimeSettings,
    cache_directory,
    copilot_token_directory,
    make_proxy_key,
    reserve_loopback_port,
    state_directory,
)

SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization[\"':=\s]+bearer\s+)[^\s\"']+"),
    re.compile(r"(?i)(access[_-]?token[\"':=\s]+)[^\s,\"'}]+"),
    re.compile(r"(?i)(api[_-]?key[\"':=\s]+)[^\s,\"'}]+"),
    re.compile(r"sk-prx-[A-Za-z0-9_-]+"),
]
AUTH_INSTRUCTION_MARKERS = ("github.com/login/device", "to authenticate")


def redact(text: str) -> str:
    result = text
    for pattern in SECRET_PATTERNS:
        if pattern.pattern.startswith("sk-prx"):
            result = pattern.sub("[REDACTED]", result)
        else:
            result = pattern.sub(r"\1[REDACTED]", result)
    return result


def is_auth_instruction(text: str) -> bool:
    lowered = text.lower()
    return all(marker in lowered for marker in AUTH_INSTRUCTION_MARKERS)


def resolve_binary(name: str, env_override: str) -> str | None:
    override = os.environ.get(env_override)
    if override:
        return override
    return shutil.which(name)


def litellm_command() -> list[str]:
    binary = resolve_binary("litellm", "PRX_LITELLM_BIN")
    if binary:
        return [binary]
    return [sys.executable, "-m", "litellm"]


def create_runtime_settings(model: str, models: dict[str, str] | None = None) -> RuntimeSettings:
    cache_root = cache_directory()
    runtime_dir = Path(tempfile.mkdtemp(prefix="run-", dir=cache_root))
    runtime_dir.chmod(0o700)
    marker = runtime_dir / ".prx-runtime"
    marker.write_text(json.dumps({"pid": os.getpid()}) + "\n", encoding="utf-8")
    marker.chmod(0o600)
    reservation, port = reserve_loopback_port()
    reservation.close()
    return RuntimeSettings(
        model=model,
        port=port,
        proxy_key=make_proxy_key(),
        token_directory=copilot_token_directory(),
        config_path=runtime_dir / "litellm.json",
        log_path=runtime_dir / "proxy.log",
        models=models or {},
        runtime_info_path=state_directory() / "proxy-runtime.json",
    )


class ProxyProcess:
    def __init__(self, settings: RuntimeSettings, *, show_logs: bool = False) -> None:
        self.settings = settings
        self.show_logs = show_logs
        self.process: subprocess.Popen[str] | None = None
        self._log_file: object | None = None
        self._pump_thread: threading.Thread | None = None

    def start(self, timeout: float = 90.0) -> None:
        write_litellm_config(self.settings)
        command = [
            *litellm_command(),
            "--config",
            str(self.settings.config_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(self.settings.port),
        ]
        self._log_file = self.settings.log_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            command,
            env=build_proxy_environment(self.settings),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self._write_runtime_info()
        self._pump_thread = threading.Thread(target=self._pump_logs, daemon=True)
        self._pump_thread.start()
        self._wait_until_ready(timeout)

    def _pump_logs(self) -> None:
        if self.process is None or self.process.stdout is None or self._log_file is None:
            return
        for line in self.process.stdout:
            safe_line = redact(line)
            self._log_file.write(safe_line)  # type: ignore[attr-defined]
            self._log_file.flush()  # type: ignore[attr-defined]
            if self.show_logs:
                print(f"[proxy] {safe_line}", end="", file=sys.stderr)
            elif is_auth_instruction(safe_line):
                print(f"[proxy-auth] {safe_line}", end="", file=sys.stderr)

    def _wait_until_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        url = f"{self.settings.base_url}/health/liveliness"
        headers = {"Authorization": f"Bearer {self.settings.proxy_key}"}
        last_error = "not ready"
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError(
                    f"LiteLLM exited with status {self.process.returncode}; "
                    f"see {self.settings.log_path}"
                )
            try:
                response = httpx.get(url, headers=headers, timeout=0.5)
                if response.status_code < 500:
                    return
                last_error = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                last_error = str(exc)
            time.sleep(0.1)
        log_tail = self._read_log_tail()
        detail = f"\nLast redacted proxy log lines:\n{log_tail}" if log_tail else ""
        raise RuntimeError(
            f"LiteLLM did not become ready within {timeout:.0f}s ({last_error}).{detail}"
        )

    def _read_log_tail(self, lines: int = 30) -> str:
        if self._log_file is not None:
            self._log_file.flush()  # type: ignore[attr-defined]
        try:
            content = self.settings.log_path.read_text(encoding="utf-8")
        except OSError:
            return ""
        return "\n".join(content.splitlines()[-lines:])

    def stop(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            self._close_log()
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
        finally:
            self._remove_runtime_info()
            self._close_log()

    def wait(self) -> int:
        if self.process is None:
            raise RuntimeError("LiteLLM has not been started")
        return self.process.wait()

    def _write_runtime_info(self) -> None:
        if self.process is None or self.settings.runtime_info_path is None:
            return
        path = self.settings.runtime_info_path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "pid": self.process.pid,
                    "port": self.settings.port,
                    "proxy_key": self.settings.proxy_key,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)

    def _remove_runtime_info(self) -> None:
        path = self.settings.runtime_info_path
        if path is None or not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if self.process is not None and payload.get("pid") == self.process.pid:
            path.unlink(missing_ok=True)

    def _close_log(self) -> None:
        if self._pump_thread is not None:
            self._pump_thread.join(timeout=1)
        if self._log_file is not None:
            self._log_file.close()  # type: ignore[attr-defined]
            self._log_file = None


@contextmanager
def running_proxy(settings: RuntimeSettings, *, show_logs: bool = False) -> Iterator[ProxyProcess]:
    proxy = ProxyProcess(settings, show_logs=show_logs)
    try:
        proxy.start()
        yield proxy
    finally:
        proxy.stop()


def run_interactive_child(command: list[str], env: dict[str, str]) -> int:
    child = subprocess.Popen(command, env=env)
    old_sigint = signal.getsignal(signal.SIGINT)
    old_sigterm = signal.getsignal(signal.SIGTERM)

    def forward(signum: int, _frame: FrameType | None) -> None:
        if child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGINT, forward)
    signal.signal(signal.SIGTERM, forward)
    try:
        return child.wait()
    finally:
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)
