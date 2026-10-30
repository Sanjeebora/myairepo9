"""How the SSH channel answers sudo's password prompt."""
import pytest

from dbaops_api.jumphost import pump
from dbaops_api.runner import MARKER, SUDO_PROMPT, CommandTimeout


class FakeChannel:
    """Plays back (stream, bytes) chunks; a chunk of ("wait_stdin", None) blocks until
    stdin is answered or closed, like sudo waiting on its password."""

    def __init__(self, chunks, exit_code=0):
        self.chunks = list(chunks)
        self.exit_code = exit_code
        self.sent = b""
        self.stdin_closed = False
        self.pending = []

    def _advance(self):
        while self.chunks and not self.pending:
            kind, data = self.chunks[0]
            if kind == "wait_stdin":
                if not (self.sent or self.stdin_closed):
                    return
                self.chunks.pop(0)
                continue
            self.pending.append(self.chunks.pop(0))

    def recv_ready(self):
        self._advance()
        return bool(self.pending) and self.pending[0][0] == "out"

    def recv_stderr_ready(self):
        self._advance()
        return bool(self.pending) and self.pending[0][0] == "err"

    def recv(self, n):
        return self.pending.pop(0)[1]

    recv_stderr = recv

    def sendall(self, data):
        assert not self.stdin_closed
        self.sent += data

    def shutdown_write(self):
        self.stdin_closed = True

    def exit_status_ready(self):
        self._advance()
        return not self.chunks and not self.pending

    def recv_exit_status(self):
        return self.exit_code


def test_password_sent_when_sudo_asks_and_prompt_removed():
    ch = FakeChannel([("err", SUDO_PROMPT.encode()), ("wait_stdin", None),
                      ("out", f"{MARKER}\nok\n".encode())])
    res = pump(ch, "Secret#123", timeout=5, sudo_prompt_wait=5)
    assert ch.sent == b"Secret#123\n"
    assert ch.stdin_closed
    assert SUDO_PROMPT.encode() not in res.stderr
    assert res.stdout.endswith(b"ok\n")


def test_no_password_sent_when_sudo_does_not_ask():
    ch = FakeChannel([("out", f"banner\n{MARKER}\nok\n".encode())])
    pump(ch, "Secret#123", timeout=5, sudo_prompt_wait=5)
    assert ch.sent == b""
    assert ch.stdin_closed


def test_second_prompt_is_not_answered():
    ch = FakeChannel([("err", SUDO_PROMPT.encode()), ("wait_stdin", None),
                      ("err", b"Sorry, try again.\n" + SUDO_PROMPT.encode()),
                      ("err", b"sudo: 1 incorrect password attempt\n")], exit_code=1)
    res = pump(ch, "wrong", timeout=5, sudo_prompt_wait=5)
    assert ch.sent == b"wrong\n"
    assert res.returncode == 1
    assert b"incorrect password" in res.stderr
    assert SUDO_PROMPT.encode() not in res.stderr


def test_stdin_closed_if_nothing_happens():
    ch = FakeChannel([("wait_stdin", None), ("out", b"done\n")])
    pump(ch, "pw", timeout=5, sudo_prompt_wait=0)
    assert ch.sent == b"" and ch.stdin_closed


def test_timeout():
    ch = FakeChannel([("wait_stdin", None), ("out", b"never")])
    ch.shutdown_write = lambda: None  # sudo keeps waiting
    with pytest.raises(CommandTimeout):
        pump(ch, "pw", timeout=0, sudo_prompt_wait=100)
