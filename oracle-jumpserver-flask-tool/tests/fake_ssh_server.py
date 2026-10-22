"""A local paramiko SSH server whose shell is the fake jump server (bash + tests/fakebin).

Used by the tests, and runnable on Linux/macOS for a manual demo:
    python tests/fake_ssh_server.py --port 2222
then log in to 127.0.0.1:2222 as jumpuser / S3cret-user-pw.
"""
from __future__ import annotations

import argparse
import select
import socket
import sys
import tempfile
import threading
from pathlib import Path

import paramiko

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import USER_PASSWORD, PtyChannel, fake_env  # noqa: E402


class _Server(paramiko.ServerInterface):
    def __init__(self, username: str, password: str):
        self.username, self.password = username, password
        self.shell_requested = threading.Event()

    def check_auth_password(self, username, password):
        ok = username == self.username and password == self.password
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, *args):
        return True

    def check_channel_shell_request(self, channel):
        self.shell_requested.set()
        return True


def _bridge(chan: paramiko.Channel, pty: PtyChannel) -> None:
    try:
        while not chan.closed:
            r, _, _ = select.select([chan, pty.fd], [], [], 0.2)
            if chan in r:
                data = chan.recv(65536)
                if not data:
                    break
                pty.send(data)
            if pty.fd in r:
                data = pty.recv(65536)
                if not data:
                    break
                chan.sendall(data)
    except (OSError, EOFError, paramiko.SSHException):
        pass  # client or server went away during teardown
    finally:
        try:
            chan.send_exit_status(0)
        except (OSError, EOFError, paramiko.SSHException):
            pass
        chan.close()
        pty.close()


class FakeSSHServer:
    def __init__(self, state: Path, port: int = 0, username: str = "jumpuser",
                 password: str = USER_PASSWORD, **env: str):
        self.state = state
        self.env = env
        self.username, self.password = username, password
        self.host_key = paramiko.RSAKey.generate(2048)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._transports: list[paramiko.Transport] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> "FakeSSHServer":
        self._thread.start()
        return self

    def _serve(self) -> None:
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                client, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            t = paramiko.Transport(client)
            t.add_server_key(self.host_key)
            server = _Server(self.username, self.password)
            try:
                t.start_server(server=server)
            except paramiko.SSHException:
                continue
            self._transports.append(t)
            threading.Thread(target=self._handle, args=(t, server), daemon=True).start()

    def _handle(self, t: paramiko.Transport, server: _Server) -> None:
        chan = t.accept(20)
        if chan is None:
            return
        server.shell_requested.wait(10)
        _bridge(chan, PtyChannel(fake_env(self.state, **self.env)))

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(2)
        for t in self._transports:
            t.close()
        self.sock.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=2222)
    p.add_argument("--sudo-password", action="store_true", help="make sudo ask for the user's password")
    a = p.parse_args()
    env = {"FAKE_SUDO_PASSWORD": USER_PASSWORD} if a.sudo_password else {}
    srv = FakeSSHServer(Path(tempfile.mkdtemp(prefix="fakejump-")), a.port, **env).start()
    print(f"Fake jump server on 127.0.0.1:{srv.port} (jumpuser / {USER_PASSWORD}); Ctrl+C to stop")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.stop()
