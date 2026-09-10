from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import MagicMock

import httpx
import pytest
from typer.testing import CliRunner

from prx import __version__
from prx.cli import app
from prx.diagnostics import Check
from prx.keys import load_proxy_key, setup_proxy_key
from prx.runtime import ProxyProcess, create_runtime_settings

runner = CliRunner()


def test_version(monkeypatch) -> None:
    monkeypatch.setattr("prx.cli.resolve_binary", lambda *_args: None)
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert f"prx {__version__}" in result.stdout


def test_doctor_fails_when_required_component_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        "prx.cli.collect_checks",
        lambda: [
            Check("Codex CLI", False, "missing"),
            Check("LiteLLM", True, "1.0"),
        ],
    )
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "[WARN] Codex CLI: missing" in result.stdout


def test_models_is_honest_about_discovery() -> None:
    result = runner.invoke(app, ["models"])
    assert result.exit_code == 0
    assert "does not expose authoritative account model discovery" in result.stdout


def test_codex_rejects_bypass_before_proxy_start(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PRX_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("PRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr("prx.cli.resolve_binary", lambda *_args: "/usr/bin/codex")
    result = runner.invoke(app, ["codex", "--", "--model", "other"])
    assert result.exit_code == 2
    assert "would bypass the prx provider" in result.stderr


@pytest.fixture
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("PRX_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("PRX_STATE_DIR", str(tmp_path / "state"))
    reservation = MagicMock()
    monkeypatch.setattr("prx.runtime.reserve_loopback_port", lambda _port: (reservation, 43210))
    return tmp_path / "state"


def test_setup_preserves_key_until_explicit_rotation(isolated_runtime) -> None:
    initial = runner.invoke(app, ["setup"])
    assert initial.exit_code == 0, initial.output
    first_key = load_proxy_key()

    repeated = runner.invoke(app, ["setup"])
    assert repeated.exit_code == 0, repeated.output
    assert load_proxy_key() == first_key

    rotated = runner.invoke(app, ["setup", "--rotate-key"])
    assert rotated.exit_code == 0, rotated.output
    second_key = load_proxy_key()
    assert second_key != first_key
    assert "Restart the proxy" in rotated.stdout
    assert "update the API key in your clients" in rotated.stdout
    for result in (initial, repeated, rotated):
        assert first_key not in result.output
        assert second_key not in result.output


@pytest.mark.parametrize("command", [["setup"], ["setup", "--rotate-key"], ["proxy"]])
def test_invalid_saved_key_is_reported_without_disclosing_it(isolated_runtime, command) -> None:
    isolated_runtime.mkdir()
    path = isolated_runtime / "proxy-key"
    invalid_key = "invalid-secret-value"
    path.write_text(invalid_key)

    result = runner.invoke(app, command)

    assert result.exit_code == 1
    assert "proxy key" in result.stderr.lower()
    assert invalid_key not in result.output
    assert path.read_text() == invalid_key


def test_proxy_requires_setup_before_creating_runtime(isolated_runtime, monkeypatch) -> None:
    create_settings = MagicMock()
    monkeypatch.setattr("prx.cli.create_runtime_settings", create_settings)

    result = runner.invoke(app, ["proxy"])

    assert result.exit_code == 1
    assert "prx setup" in result.stderr
    create_settings.assert_not_called()


def test_proxy_reuses_saved_key_on_each_start(isolated_runtime, monkeypatch) -> None:
    setup_proxy_key()
    saved_key = load_proxy_key()
    started = []

    @contextmanager
    def fake_proxy(settings, **_kwargs):
        started.append(settings)
        yield MagicMock()

    monkeypatch.setattr("prx.cli.running_proxy", fake_proxy)
    monkeypatch.setattr("prx.cli.load_models", lambda: {"test": "test"})
    for _ in range(2):
        result = runner.invoke(app, ["proxy"])
        assert result.exit_code == 0, result.output

    assert len(started) == 2
    assert all(settings.proxy_key == saved_key for settings in started)


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_setenv_uses_running_key_after_disk_rotation(isolated_runtime, shell) -> None:
    setup_proxy_key()
    running_key = load_proxy_key()
    proxy = ProxyProcess(create_runtime_settings("test", proxy_key=running_key))
    proxy.process = MagicMock(pid=os.getpid())
    proxy._write_runtime_info()

    setup_proxy_key(rotate=True)
    rotated_key = load_proxy_key()
    result = runner.invoke(app, ["proxy", "setenv", "--shell", shell])

    assert result.exit_code == 0, result.output
    assert running_key in result.stdout
    assert rotated_key not in result.stdout
    if shell == "fish":
        assert f"set -gx PRX_PROXY_KEY {running_key}" in result.stdout
    else:
        assert f"export PRX_PROXY_KEY={running_key}" in result.stdout


def test_setenv_requires_running_proxy_even_with_saved_key(isolated_runtime) -> None:
    setup_proxy_key()
    result = runner.invoke(app, ["proxy", "setenv"])
    assert result.exit_code != 0
    assert "No running prx proxy was found" in result.output
    assert load_proxy_key() not in result.output


@pytest.mark.parametrize("command", ["auth", "codex"])
@pytest.mark.parametrize("saved_state", ["missing", "invalid", "valid"])
def test_auth_and_codex_keep_ephemeral_keys(
    isolated_runtime, monkeypatch, command, saved_state
) -> None:
    if saved_state == "valid":
        setup_proxy_key()
    elif saved_state == "invalid":
        isolated_runtime.mkdir()
        (isolated_runtime / "proxy-key").write_text("invalid")
    key_path = isolated_runtime / "proxy-key"
    saved_contents = key_path.read_bytes() if key_path.exists() else None
    started = []

    @contextmanager
    def fake_proxy(settings, **_kwargs):
        started.append(settings)
        yield MagicMock()

    monkeypatch.setattr("prx.cli.running_proxy", fake_proxy)
    monkeypatch.setattr("prx.cli.resolve_binary", lambda *_args: "/usr/bin/codex")
    child = MagicMock(return_value=0)
    monkeypatch.setattr("prx.cli.run_interactive_child", child)
    post = MagicMock(return_value=httpx.Response(200))
    monkeypatch.setattr("prx.cli.httpx.post", post)

    for _ in range(2):
        result = runner.invoke(app, [command])
        assert result.exit_code == 0, result.output

    assert len(started) == 2
    assert started[0].proxy_key != started[1].proxy_key
    for settings in started:
        assert settings.proxy_key.startswith("sk-prx-")
        assert not settings.config_path.parent.exists()
        if saved_contents is not None:
            assert settings.proxy_key != saved_contents.decode().strip()
    if saved_contents is None:
        assert not key_path.exists()
    else:
        assert key_path.read_bytes() == saved_contents
    if command == "auth":
        assert post.call_count == 2
        assert post.call_args.kwargs["headers"]["Authorization"] == (
            f"Bearer {started[-1].proxy_key}"
        )
    else:
        assert child.call_count == 2
        assert child.call_args.args[1]["PRX_PROXY_KEY"] == started[-1].proxy_key
