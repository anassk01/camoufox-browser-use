"""Executable selection, launch environment, and persistent profile ownership."""

from __future__ import annotations

import errno
import os
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import Self

try:
    import fcntl
except ImportError:
    fcntl = None


class ProfileLockError(RuntimeError):
    """A persistent profile already has an active lock."""


class ProfileLock:
    """Hold a non-blocking lock for the lifetime of a persistent profile.

    On POSIX, the lockfile stays in place so all processes lock the same inode.
    Where flock is unavailable, an interrupted owner can leave an exclusive
    lockfile behind. Check its PID before removing it manually.
    """

    def __init__(self, user_data_dir: str | Path) -> None:
        self.path = Path(user_data_dir).expanduser().resolve() / '.camoufox_browseruse.lock'
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            raise ProfileLockError(f'Profile lock is already held: {self.path}')

        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT
        if fcntl is None:
            flags |= os.O_EXCL
        else:
            flags |= getattr(os, 'O_NOFOLLOW', 0)

        try:
            fd = os.open(self.path, flags, 0o600)
        except FileExistsError as exc:
            raise ProfileLockError(f'Profile is already locked: {self.path}') from exc

        try:
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno in {errno.EACCES, errno.EAGAIN}:
                        raise ProfileLockError(f'Profile is already locked: {self.path}') from exc
                    raise
            os.ftruncate(fd, 0)
            os.write(fd, f'{os.getpid()}\n'.encode('ascii'))
        except BaseException:
            self._close_file(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            self._close_file(fd)

    def _close_file(self, fd: int) -> None:
        try:
            os.close(fd)
        finally:
            if fcntl is None:
                self.path.unlink(missing_ok=True)

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def resolve_executable_path(executable_path: str | Path | None = None) -> str | None:
    """Validate a caller or environment override, or let Camoufox choose."""
    source = 'executable_path'
    if executable_path is None:
        source = 'CAMOUFOX_EXECUTABLE_PATH'
        executable_path = os.environ.get(source)
    if executable_path is None:
        return None

    path = Path(executable_path).expanduser()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise FileNotFoundError(f'{source} must point to an executable file: {path}')
    return str(path)


def prepare_launch_environment(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy the launch environment and disable Wayland to avoid Firefox hangs."""
    env = dict(os.environ if base_env is None else base_env)
    env['MOZ_ENABLE_WAYLAND'] = '0'
    env.pop('WAYLAND_DISPLAY', None)
    return env
