from __future__ import annotations

import multiprocessing
import os
import re
import stat
from pathlib import Path

import pytest

from prx import keys

_KEY_RE = re.compile(r"sk-prx-[A-Za-z0-9_-]{43}\Z")


def test_setup_uses_override_and_publishes_private_files(monkeypatch, tmp_path) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("PRX_STATE_DIR", str(state))

    path = keys.setup_proxy_key()

    assert path == state / "proxy-key"
    assert _KEY_RE.fullmatch(keys.load_proxy_key())
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((state / ".proxy-key.lock").stat().st_mode) == 0o600


def test_setup_is_idempotent_and_rotate_replaces_key(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PRX_STATE_DIR", str(tmp_path))

    path = keys.setup_proxy_key()
    first = keys.load_proxy_key()
    assert keys.setup_proxy_key() == path
    assert keys.load_proxy_key() == first

    assert keys.setup_proxy_key(rotate=True) == path
    second = keys.load_proxy_key()
    assert _KEY_RE.fullmatch(second)
    assert second != first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_load_missing_key_has_setup_instruction(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PRX_STATE_DIR", str(tmp_path))

    with pytest.raises(ValueError, match=r"run `prx setup`"):
        keys.load_proxy_key()


@pytest.mark.parametrize(
    "contents",
    [
        b"",
        b"sk-prx-short",
        b"sk-prx-" + b"a" * 43 + b"\n\n",
        b"sk-prx-" + b"!" * 43,
        b"sk-prx-" + b"\xff" * 43,
        b"sk-prx-" + b"a" * 43 + b"x" * 4096,
    ],
)
def test_invalid_key_is_rejected_without_exposing_contents(
    monkeypatch, tmp_path, contents: bytes
) -> None:
    monkeypatch.setenv("PRX_STATE_DIR", str(tmp_path))
    path = tmp_path / "proxy-key"
    path.write_bytes(contents)
    path.chmod(0o600)

    with pytest.raises(ValueError, match="invalid proxy key file") as error:
        keys.load_proxy_key()
    if contents:
        assert contents.decode("ascii", errors="replace") not in str(error.value)
    with pytest.raises(ValueError, match="invalid proxy key file"):
        keys.setup_proxy_key()
    with pytest.raises(ValueError, match="invalid proxy key file"):
        keys.setup_proxy_key(rotate=True)
    assert path.read_bytes() == contents


def test_load_accepts_one_final_newline(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PRX_STATE_DIR", str(tmp_path))
    key = "sk-prx-" + "a" * 43
    path = tmp_path / "proxy-key"
    path.write_text(key + "\n", encoding="ascii")
    path.chmod(0o600)

    assert keys.load_proxy_key() == key


def test_load_rejects_symlink_and_nonregular_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PRX_STATE_DIR", str(tmp_path))
    target = tmp_path / "target"
    target.write_text("sk-prx-" + "a" * 43, encoding="ascii")
    (tmp_path / "proxy-key").symlink_to(target)
    with pytest.raises(ValueError, match="must not be a symlink"):
        keys.load_proxy_key()

    (tmp_path / "proxy-key").unlink()
    (tmp_path / "proxy-key").mkdir()
    with pytest.raises(ValueError, match="regular file"):
        keys.load_proxy_key()


def test_concurrent_processes_publish_one_key(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PRX_STATE_DIR", str(tmp_path))
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    barrier = context.Barrier(8)
    processes = [
        context.Process(
            target=_setup_worker, args=(str(tmp_path), queue, barrier)
        )
        for _ in range(8)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    results = [queue.get(timeout=2) for _ in processes]
    names, worker_keys = zip(*results, strict=True)
    assert all(name == "proxy-key" for name in names)
    assert all(_KEY_RE.fullmatch(key) for key in worker_keys)
    assert len(set(worker_keys)) == 1
    assert keys.load_proxy_key() == worker_keys[0]
    assert not _temporary_files(tmp_path)


def test_failed_rotation_preserves_existing_key(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PRX_STATE_DIR", str(tmp_path))
    path = keys.setup_proxy_key()
    original = keys.load_proxy_key()

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("publication failed")

    monkeypatch.setattr(keys.os, "replace", fail_replace)
    with pytest.raises(OSError, match="publication failed"):
        keys.setup_proxy_key(rotate=True)

    assert path.read_text(encoding="ascii") == original
    assert keys.load_proxy_key() == original
    assert not _temporary_files(tmp_path)


def test_failed_initial_publication_leaves_no_partial_key(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PRX_STATE_DIR", str(tmp_path))

    def fail_link(_source: Path, _destination: Path) -> None:
        raise OSError("publication failed")

    monkeypatch.setattr(keys.os, "link", fail_link)
    with pytest.raises(OSError, match="publication failed"):
        keys.setup_proxy_key()

    assert not (tmp_path / "proxy-key").exists()
    assert not _temporary_files(tmp_path)


def test_failed_initial_write_leaves_no_partial_key(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PRX_STATE_DIR", str(tmp_path))

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("write failed")

    monkeypatch.setattr(keys.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="write failed"):
        keys.setup_proxy_key()

    assert not (tmp_path / "proxy-key").exists()
    assert not _temporary_files(tmp_path)


def test_failed_rotation_write_preserves_existing_key(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PRX_STATE_DIR", str(tmp_path))
    path = keys.setup_proxy_key()
    original = keys.load_proxy_key()

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("write failed")

    monkeypatch.setattr(keys.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="write failed"):
        keys.setup_proxy_key(rotate=True)

    assert path.read_text(encoding="ascii") == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not _temporary_files(tmp_path)


@pytest.mark.parametrize("rotate", [False, True])
def test_key_is_complete_and_private_before_publication(monkeypatch, tmp_path, rotate) -> None:
    monkeypatch.setenv("PRX_STATE_DIR", str(tmp_path))
    path = tmp_path / "proxy-key"
    original = None
    if rotate:
        keys.setup_proxy_key()
        original = keys.load_proxy_key()
    operation_name = "replace" if rotate else "link"
    publish = getattr(keys.os, operation_name)
    published = []

    def inspect_publication(source, destination):
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
        assert _KEY_RE.fullmatch(source.read_text())
        if original is None:
            assert not path.exists()
        else:
            assert path.read_text() == original
        publish(source, destination)
        published.append(path.read_text())

    monkeypatch.setattr(keys.os, operation_name, inspect_publication)
    keys.setup_proxy_key(rotate=rotate)

    assert published == [keys.load_proxy_key()]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def _setup_worker(state: str, queue, barrier) -> None:
    os.environ["PRX_STATE_DIR"] = state
    try:
        barrier.wait(timeout=10)
        path = keys.setup_proxy_key()
        queue.put((path.name, keys.load_proxy_key()))
    except BaseException as error:
        queue.put(f"error: {error}")
        raise


def _temporary_files(directory: Path) -> list[Path]:
    return [path for path in directory.glob(".proxy-key.*") if path.name != ".proxy-key.lock"]
