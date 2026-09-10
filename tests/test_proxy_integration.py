from __future__ import annotations

import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import httpx
import pytest

from prx.runtime import ProxyProcess
from prx.settings import RuntimeSettings


@pytest.mark.integration
def test_proxy_process_waits_for_health_and_stops(monkeypatch, tmp_path: Path) -> None:
    fake_litellm = tmp_path / "fake-litellm"
    fake_litellm.write_text(
        f"""#!{sys.executable}
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer

parser = argparse.ArgumentParser()
parser.add_argument("--config")
parser.add_argument("--host")
parser.add_argument("--port", type=int)
args = parser.parse_args()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/health/liveliness" else 404)
        self.end_headers()
    def log_message(self, format, *args):
        return

HTTPServer((args.host, args.port), Handler).serve_forever()
""",
        encoding="utf-8",
    )
    fake_litellm.chmod(0o700)

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()

    settings = RuntimeSettings(
        model="gpt-test",
        port=port,
        proxy_key="sk-prx-integration-secret",
        token_directory=tmp_path / "tokens",
        config_path=tmp_path / "litellm.json",
        log_path=tmp_path / "proxy.log",
    )
    settings.token_directory.mkdir()
    monkeypatch.setenv("PRX_LITELLM_BIN", str(fake_litellm))

    proxy = ProxyProcess(settings)
    proxy.start(timeout=5)
    try:
        assert proxy.process is not None
        assert proxy.process.poll() is None
    finally:
        proxy.stop()

    assert proxy.process is not None
    assert proxy.process.poll() is not None
    assert "integration-secret" not in settings.log_path.read_text()


@pytest.mark.integration
def test_standalone_proxy_uses_persistent_key_and_requires_restart_after_rotation(
    tmp_path: Path,
) -> None:
    fake_litellm = tmp_path / "fake-litellm"
    fake_litellm.write_text(
        f"""#!{sys.executable}
import hmac
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--config")
parser.add_argument("--host")
parser.add_argument("--port", type=int)
args = parser.parse_args()

expected = os.environ["PRX_PROXY_KEY"].encode()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        received = self.headers.get("Authorization", "").encode()
        authorized = hmac.compare_digest(received, b"Bearer " + expected)
        status = 200 if self.path == "/health/liveliness" and authorized else 401
        self.send_response(status)
        self.end_headers()

    def log_message(self, format, *args):
        return

HTTPServer((args.host, args.port), Handler).serve_forever()
""",
        encoding="utf-8",
    )
    fake_litellm.chmod(0o700)

    state_dir = tmp_path / "state"
    cache_dir = tmp_path / "cache"
    models_file = tmp_path / "models.json"
    models_file.write_text('{"models": {"test-model": "test-model"}}\n', encoding="utf-8")
    source_dir = Path(__file__).resolve().parents[1] / "src"
    env = os.environ.copy()
    env.update(
        {
            "PRX_STATE_DIR": str(state_dir),
            "PRX_CACHE_DIR": str(cache_dir),
            "PRX_LITELLM_BIN": str(fake_litellm),
            "PRX_MODELS_FILE": str(models_file),
            "PYTHONPATH": str(source_dir),
        }
    )
    cli = [sys.executable, "-m", "prx.cli"]
    key_path = state_dir / "proxy-key"
    runtime_info_path = state_dir / "proxy-runtime.json"

    setup = subprocess.run(
        [*cli, "setup"],
        env=env,
        cwd=source_dir.parent,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert setup.returncode == 0
    first_key = key_path.read_text(encoding="utf-8").strip()
    assert first_key.startswith("sk-prx-")

    running: subprocess.Popen[str] | None = None
    try:
        running, first_runtime = _start_standalone_proxy(
            cli, env, source_dir.parent, runtime_info_path
        )
        assert first_runtime["proxy_key"] == first_key
        _assert_health(int(first_runtime["port"]), first_key, expected=200)
        _assert_health(int(first_runtime["port"]), "sk-prx-wrong", expected=401)

        setenv = subprocess.run(
            [*cli, "proxy", "setenv"],
            env=env,
            cwd=source_dir.parent,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert setenv.returncode == 0
        assert f"export PRX_PROXY_KEY={shlex.quote(first_key)}" in setenv.stdout
        assert f'base_url = "http://127.0.0.1:{first_runtime["port"]}/v1"' in setenv.stdout

        rotate = subprocess.run(
            [*cli, "setup", "--rotate-key"],
            env=env,
            cwd=source_dir.parent,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert rotate.returncode == 0
        rotated_key = key_path.read_text(encoding="utf-8").strip()
        assert rotated_key.startswith("sk-prx-")
        assert rotated_key != first_key

        still_old = subprocess.run(
            [*cli, "proxy", "setenv"],
            env=env,
            cwd=source_dir.parent,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert still_old.returncode == 0
        assert f"export PRX_PROXY_KEY={shlex.quote(first_key)}" in still_old.stdout
        assert f"export PRX_PROXY_KEY={shlex.quote(rotated_key)}" not in still_old.stdout
        _assert_health(int(first_runtime["port"]), first_key, expected=200)
        _assert_health(int(first_runtime["port"]), rotated_key, expected=401)

        _stop_standalone_proxy(running, runtime_info_path)
        running = None
        assert not runtime_info_path.exists()

        running, rotated_runtime = _start_standalone_proxy(
            cli, env, source_dir.parent, runtime_info_path
        )
        assert rotated_runtime["proxy_key"] == rotated_key
        _assert_health(int(rotated_runtime["port"]), rotated_key, expected=200)
        _assert_health(int(rotated_runtime["port"]), first_key, expected=401)
        _stop_standalone_proxy(running, runtime_info_path)
        running = None
        assert not runtime_info_path.exists()

        running, unchanged_runtime = _start_standalone_proxy(
            cli, env, source_dir.parent, runtime_info_path
        )
        assert unchanged_runtime["proxy_key"] == rotated_key
        assert key_path.read_text(encoding="utf-8").strip() == rotated_key
        _assert_health(int(unchanged_runtime["port"]), rotated_key, expected=200)
    finally:
        if running is not None:
            _stop_standalone_proxy(running, runtime_info_path)


def _start_standalone_proxy(
    cli: list[str],
    env: dict[str, str],
    cwd: Path,
    runtime_info_path: Path,
) -> tuple[subprocess.Popen[str], dict[str, object]]:
    port = _ephemeral_port()
    process = subprocess.Popen(
        [*cli, "proxy", "--port", str(port)],
        env=env,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if runtime_info_path.is_file():
            try:
                payload = json.loads(runtime_info_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict) and payload.get("pid") and payload.get("port"):
                if process.poll() is None:
                    try:
                        _wait_for_health(
                            int(payload["port"]), str(payload["proxy_key"]), timeout=2
                        )
                    except AssertionError:
                        pass
                    else:
                        return process, payload
                else:
                    break
        if process.poll() is not None:
            break
        time.sleep(0.05)
    _stop_standalone_proxy(process, runtime_info_path)
    raise AssertionError("standalone proxy did not publish runtime information")


def _stop_standalone_proxy(process: subprocess.Popen[str], runtime_info_path: Path) -> None:
    child_pid: int | None = None
    try:
        payload = json.loads(runtime_info_path.read_text(encoding="utf-8"))
        child_pid = int(payload["pid"])
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        pass

    if process.poll() is None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGINT)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)

    if child_pid is not None:
        with suppress(ProcessLookupError):
            os.killpg(child_pid, signal.SIGTERM)
        child_deadline = time.monotonic() + 2
        while time.monotonic() < child_deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            except PermissionError:
                break
            time.sleep(0.05)
        else:
            with suppress(ProcessLookupError):
                os.killpg(child_pid, signal.SIGKILL)
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()


def _assert_health(port: int, proxy_key: str, *, expected: int) -> None:
    response = httpx.get(
        f"http://127.0.0.1:{port}/health/liveliness",
        headers={"Authorization": f"Bearer {proxy_key}"},
        timeout=2,
        trust_env=False,
    )
    assert response.status_code == expected


def _wait_for_health(port: int, proxy_key: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(
                f"http://127.0.0.1:{port}/health/liveliness",
                headers={"Authorization": f"Bearer {proxy_key}"},
                timeout=0.2,
                trust_env=False,
            )
        except httpx.HTTPError:
            time.sleep(0.05)
            continue
        if response.status_code == 200:
            return
        time.sleep(0.05)
    raise AssertionError("standalone proxy health endpoint did not become ready")


def _ephemeral_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()
