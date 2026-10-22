from __future__ import annotations

import os
import select
import signal
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from oracle_jump_tool.config import Settings  # noqa: E402

FAKEBIN = Path(__file__).resolve().parent / "fakebin"
USER_PASSWORD = "S3cret-user-pw"
CCPS_PASSWORD = "bake6Pies"

needs_pty = pytest.mark.skipif(os.name != "posix", reason="fake jump server needs a POSIX pty")


class PtyChannel:
    """A paramiko-Channel-like object backed by a real bash in a pseudo-terminal."""

    def __init__(self, env: dict, noediting: bool = False):
        import pty

        for f in FAKEBIN.iterdir():
            f.chmod(0o755)
        with warnings.catch_warnings():
            # The child execs bash immediately, so the multi-threaded fork warning does not apply.
            warnings.simplefilter("ignore", DeprecationWarning)
            self.pid, self.fd = pty.fork()
        if self.pid == 0:  # child
            argv = ["bash", "--norc", "--noprofile"] + (["--noediting"] if noediting else []) + ["-i"]
            os.execvpe("bash", argv, env)
        self.closed = False
        self._status: int | None = None

    def send(self, data: bytes) -> int:
        if self.closed:
            return 0
        return os.write(self.fd, data)

    def recv_ready(self) -> bool:
        if self.closed:
            return False
        r, _, _ = select.select([self.fd], [], [], 0)
        return bool(r)

    def recv(self, n: int) -> bytes:
        try:
            return os.read(self.fd, n)
        except OSError:
            self.closed = True
            return b""

    def exit_status_ready(self) -> bool:
        if self._status is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self._status = status
        return self._status is not None

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.exit_status_ready() or _reap(self.pid)


def _reap(pid: int) -> None:
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def fake_env(state: Path, **overrides: str) -> dict:
    env = {
        "PATH": f"{FAKEBIN}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(state),
        "TERM": "dumb",
        "PS1": "jumpuser@jump$ ",
        "FAKE_STATE": str(state),
        "FAKE_CCPS_PASSWORD": CCPS_PASSWORD,
    }
    env.update(overrides)
    return env


@dataclass
class FakeConnection:
    channel: PtyChannel

    def close(self) -> None:
        self.channel.close()


class FakeConnector:
    """Stands in for paramiko: checks the password, then hands back a pty-backed shell."""

    def __init__(self, state: Path, password: str = USER_PASSWORD, **env: str):
        self.state = state
        self.password = password
        self.env = env
        self.connections: list[FakeConnection] = []

    def __call__(self, settings, host, port, username, password):
        from oracle_jump_tool.session import LoginError

        if password != self.password:
            raise LoginError("The jump server rejected the username or password", 401, "ssh")
        conn = FakeConnection(PtyChannel(fake_env(self.state, **self.env)))
        self.connections.append(conn)
        return conn


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings.from_env({"JT_JUMP_HOST": "jump.example.com", "JT_KNOWN_HOSTS": str(tmp_path / "kh")})
    s.banner_quiet_seconds = 1.0
    s.quiet_seconds = 5.0
    s.login_timeout = 20
    return s
