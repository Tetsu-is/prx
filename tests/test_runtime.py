from __future__ import annotations

import os
import sys

from prx.runtime import is_auth_instruction, redact, resolve_binary, run_interactive_child


def test_redact_common_secrets() -> None:
    text = (
        "Authorization: Bearer abc123\n"
        '"access_token":"token-value"\n'
        '"api_key": "key-value"\n'
        "generated sk-prx-this-is-secret"
    )
    safe = redact(text)
    assert "abc123" not in safe
    assert "token-value" not in safe
    assert "key-value" not in safe
    assert "sk-prx-this-is-secret" not in safe
    assert safe.count("[REDACTED]") == 4


def test_device_flow_instruction_is_detected() -> None:
    assert is_auth_instruction(
        "Please visit https://github.com/login/device and enter code ABCD-1234 to authenticate."
    )
    assert not is_auth_instruction("No existing access token found")


def test_resolve_binary_honors_override(monkeypatch) -> None:
    monkeypatch.setenv("PRX_TEST_BIN", "/custom/tool")
    assert resolve_binary("ignored", "PRX_TEST_BIN") == "/custom/tool"


def test_interactive_child_returns_exit_code() -> None:
    code = run_interactive_child(
        [sys.executable, "-c", "raise SystemExit(7)"],
        os.environ.copy(),
    )
    assert code == 7


def test_interactive_child_forwards_environment(tmp_path) -> None:
    output = tmp_path / "output"
    env = os.environ.copy()
    env["PRX_TEST_VALUE"] = "forwarded"
    code = run_interactive_child(
        [
            sys.executable,
            "-c",
            "import os, pathlib; pathlib.Path(os.environ['PRX_TEST_OUTPUT']).write_text("
            "os.environ['PRX_TEST_VALUE'])",
        ],
        {**env, "PRX_TEST_OUTPUT": str(output)},
    )
    assert code == 0
    assert output.read_text() == "forwarded"
