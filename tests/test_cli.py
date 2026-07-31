from __future__ import annotations

from typer.testing import CliRunner

from prx import __version__
from prx.cli import app
from prx.diagnostics import Check

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

