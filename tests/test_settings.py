from __future__ import annotations

import stat
from unittest.mock import MagicMock

from prx.settings import ensure_private_directory, make_proxy_key, reserve_loopback_port


def test_reserve_loopback_port(monkeypatch) -> None:
    fake_socket = MagicMock()
    fake_socket.getsockname.return_value = ("127.0.0.1", 43210)
    monkeypatch.setattr("prx.settings.socket.socket", lambda *_args: fake_socket)
    reservation, port = reserve_loopback_port()
    host, actual_port = reservation.getsockname()
    assert host == "127.0.0.1"
    assert actual_port == port == 43210
    fake_socket.bind.assert_called_once_with(("127.0.0.1", 0))
    reservation.close()


def test_proxy_keys_are_random_and_prefixed() -> None:
    first = make_proxy_key()
    second = make_proxy_key()
    assert first.startswith("sk-prx-")
    assert second.startswith("sk-prx-")
    assert first != second


def test_private_directory_permissions(tmp_path) -> None:
    path = ensure_private_directory(tmp_path / "state")
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o700
