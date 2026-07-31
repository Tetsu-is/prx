from __future__ import annotations

import socket
import sys
from pathlib import Path

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
