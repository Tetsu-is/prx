from __future__ import annotations

import errno
import fcntl
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from prx.settings import make_proxy_key, state_directory

_KEY_FILENAME = "proxy-key"
_LOCK_FILENAME = ".proxy-key.lock"
_KEY_PATTERN = re.compile(rb"sk-prx-[A-Za-z0-9_-]{43}\Z")
_KEY_MODE = 0o600
_MAX_KEY_FILE_BYTES = 52


def load_proxy_key() -> str:
    """Load and validate the persistent proxy key."""
    path = state_directory() / _KEY_FILENAME
    try:
        payload = _read_key_file(path)
    except FileNotFoundError:
        raise ValueError("proxy key is not configured; run `prx setup`") from None
    return payload


def setup_proxy_key(*, rotate: bool = False) -> Path:
    """Create the persistent proxy key, or replace it when ``rotate`` is true."""
    directory = state_directory()
    path = directory / _KEY_FILENAME
    with _writer_lock(directory / _LOCK_FILENAME):
        try:
            _read_key_file(path)
        except FileNotFoundError:
            return _create_initial_key(directory, path)

        os.chmod(path, _KEY_MODE, follow_symlinks=False)
        if not rotate:
            return path
        _replace_key(directory, path, make_proxy_key())
        return path


def _read_key_file(path: Path) -> str:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("proxy key file must not be a symlink")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("proxy key file must be a regular file")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError("proxy key file must not be a symlink") from None
        raise

    try:
        opened_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(opened_metadata.st_mode):
            raise ValueError("proxy key file must be a regular file")
        if opened_metadata.st_size not in {50, 51}:
            raise ValueError("invalid proxy key file")
        content = os.read(descriptor, _MAX_KEY_FILE_BYTES)
    finally:
        os.close(descriptor)

    if content.endswith(b"\n"):
        content = content[:-1]
    if not _KEY_PATTERN.fullmatch(content):
        raise ValueError("invalid proxy key file")
    return content.decode("ascii")


@contextmanager
def _writer_lock(path: Path) -> Iterator[None]:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags, _KEY_MODE)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("proxy key lock must be a regular file")
        os.fchmod(descriptor, _KEY_MODE)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _create_initial_key(directory: Path, path: Path) -> Path:
    temporary = _write_temporary_key(directory, make_proxy_key())
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            _read_key_file(path)
        _sync_directory(directory)
    finally:
        _unlink_temporary(temporary)
    return path


def _replace_key(directory: Path, path: Path, key: str) -> None:
    temporary = _write_temporary_key(directory, key)
    try:
        os.replace(temporary, path)
        _sync_directory(directory)
    finally:
        _unlink_temporary(temporary)


def _write_temporary_key(directory: Path, key: str) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=".proxy-key.", dir=directory)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, _KEY_MODE)
        stream = os.fdopen(descriptor, "wb")
        descriptor = -1
        with stream:
            stream.write(key.encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        _unlink_temporary(temporary)
        raise
    return temporary


def _unlink_temporary(path: Path) -> None:
    path.unlink(missing_ok=True)


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
