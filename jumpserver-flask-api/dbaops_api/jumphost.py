from __future__ import annotations

import logging
import socket
import threading
import time
import warnings
from pathlib import Path
from typing import Optional

with warnings.catch_warnings():
    # cryptography warns that Python 3.8 support is deprecated; it still works.
    warnings.simplefilter("ignore")
    import paramiko

from .config import JumpConfig
from .runner import MARKER, SUDO_PROMPT, CommandTimeout, Completed
from .sessions import JumpUnavailable, LoginFailed

log = logging.getLogger(__name__)


def connect(cfg: JumpConfig, username: str, password: str) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    known = Path(cfg.known_hosts).expanduser()
    if known.is_file():
        client.load_host_keys(str(known))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        # password auth falls back to keyboard-interactive, which PAM-only sshd setups use.
        client.connect(hostname=cfg.host, port=cfg.port, username=username, password=password,
                       timeout=cfg.connect_timeout, banner_timeout=cfg.connect_timeout,
                       auth_timeout=cfg.connect_timeout, look_for_keys=False, allow_agent=False)
    except paramiko.AuthenticationException:
        client.close()
        raise LoginFailed("Invalid username or password")
    except paramiko.SSHException as exc:
        client.close()
        if "not found in known_hosts" in str(exc):
            raise JumpUnavailable(
                f"The jump server host key for {cfg.host} is not in {cfg.known_hosts}. "
                f"Add it with: ssh-keyscan -p {cfg.port} {cfg.host} >> {cfg.known_hosts}")
        raise JumpUnavailable(f"SSH to {cfg.host}:{cfg.port} failed: {exc}")
    except (socket.error, OSError) as exc:
        client.close()
        raise JumpUnavailable(f"Cannot connect to {cfg.host}:{cfg.port}: {exc}")
    transport = client.get_transport()
    if transport is not None:
        transport.set_keepalive(30)
    return client


def pump(channel, password: str, timeout: int, sudo_prompt_wait: int) -> Completed:
    """Read a running command's output and answer sudo's password prompt once.

    stdin is closed after the password is sent, once the marker shows ssh is starting, or after
    sudo_prompt_wait seconds. A second prompt (wrong sudo password) then reads EOF and sudo fails
    instead of hanging. Nothing after sudo can read the password.
    """
    prompt = SUDO_PROMPT.encode()
    marker = MARKER.encode()
    out, err = bytearray(), bytearray()
    started = time.monotonic()
    deadline = started + timeout
    stdin_open = True
    while True:
        progressed = False
        while channel.recv_ready():
            out += channel.recv(65536)
            progressed = True
        while channel.recv_stderr_ready():
            err += channel.recv_stderr(65536)
            progressed = True
        if stdin_open:
            if prompt in err:
                channel.sendall((password + "\n").encode())
                err = err.replace(prompt, b"", 1)
                channel.shutdown_write()
                stdin_open = False
            elif marker in out or time.monotonic() - started > sudo_prompt_wait:
                channel.shutdown_write()
                stdin_open = False
        if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
            break
        if time.monotonic() > deadline:
            raise CommandTimeout(f"No result within {timeout}s")
        if not progressed:
            time.sleep(0.05)
    return Completed(channel.recv_exit_status(), bytes(out),
                     bytes(err.replace(prompt, b"")))


class JumpSession:
    """One person's SSH login to the jump server, reused by all of their /run requests."""

    def __init__(self, cfg: JumpConfig, username: str, password: str):
        self.cfg = cfg
        self.username = username
        self._password = password
        self._lock = threading.Lock()
        self._client: Optional[paramiko.SSHClient] = connect(cfg, username, password)

    def _transport(self):
        with self._lock:
            transport = self._client.get_transport() if self._client else None
            if transport is None or not transport.is_active():
                log.info("Reconnecting %s to the jump server", self.username)
                if self._client:
                    self._client.close()
                self._client = connect(self.cfg, self.username, self._password)
                transport = self._client.get_transport()
            return transport

    def exec(self, command: str, timeout: int) -> Completed:
        channel = self._transport().open_session(timeout=self.cfg.connect_timeout)
        try:
            channel.exec_command(command)
            return pump(channel, self._password, timeout, self.cfg.sudo_prompt_wait)
        finally:
            channel.close()

    def redact(self, text: str) -> str:
        if len(self._password) >= 4:
            return text.replace(self._password, "********")
        return text

    def close(self) -> None:
        with self._lock:
            if self._client:
                self._client.close()
                self._client = None
        self._password = ""
