import logging
import socket
import threading
import time
import warnings
from pathlib import Path

with warnings.catch_warnings():
    # Keep third-party deprecation warnings out of service logs.
    warnings.simplefilter("ignore")
    import paramiko

from .runner import CCPS_PROMPT, MARKER, SUDO_PROMPT, CommandTimeout, Completed

log = logging.getLogger(__name__)


class LoginFailed(Exception):
    pass


class JumpUnavailable(Exception):
    pass


def connect(cfg, username, password):
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    known = Path(cfg.known_hosts).expanduser()
    if known.is_file():
        client.load_host_keys(str(known))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        # password auth falls back to keyboard-interactive, which PAM-only sshd setups use.
        client.connect(hostname=cfg.jump_host, port=cfg.jump_port, username=username,
                       password=password, timeout=cfg.connect_timeout,
                       banner_timeout=cfg.connect_timeout, auth_timeout=cfg.connect_timeout,
                       look_for_keys=False, allow_agent=False)
    except paramiko.AuthenticationException:
        client.close()
        raise LoginFailed("Invalid username or password")
    except paramiko.SSHException as exc:
        client.close()
        if "not found in known_hosts" in str(exc):
            raise JumpUnavailable(
                "The jump server host key for %s is not in %s" % (
                    cfg.jump_host, cfg.known_hosts))
        raise JumpUnavailable("SSH handshake to the configured jump server failed")
    except (socket.error, OSError) as exc:
        client.close()
        raise JumpUnavailable("Cannot connect to %s:%s" % (cfg.jump_host, cfg.jump_port))
    transport = client.get_transport()
    if transport is not None:
        transport.set_keepalive(30)
    return client


def pump(channel, password, timeout, prompt_wait, ccps_secret=None,
         output_limit=1114112):
    """Read a running command's output and answer each known prompt once.

    sudo's prompt gets the person's password; the oracle shell's ccps prompt gets the configured
    ccps secret. Each answer is sent only when its own prompt appears. stdin is closed once the
    marker shows the script is starting, when a prompt repeats (wrong answer), or after prompt_wait
    seconds without a prompt, so nothing can hang waiting for input.
    """
    answers = {SUDO_PROMPT.encode(): password,
               CCPS_PROMPT.encode(): ccps_secret or ""}
    marker = MARKER.encode()
    out, err = bytearray(), bytearray()
    out_cut = False
    err_cut = False
    started = time.monotonic()
    deadline = started + timeout
    last_prompt = started
    answered = set()
    stdin_open = True
    while True:
        progressed = False
        while channel.recv_ready():
            chunk = channel.recv(65536)
            remaining = output_limit - len(out)
            if remaining > 0:
                out += chunk[:remaining]
            out_cut = out_cut or len(chunk) > max(remaining, 0)
            progressed = True
        while channel.recv_stderr_ready():
            chunk = channel.recv_stderr(65536)
            remaining = output_limit - len(err)
            if remaining > 0:
                err += chunk[:remaining]
            err_cut = err_cut or len(chunk) > max(remaining, 0)
            progressed = True
        if stdin_open:
            for prompt, answer in answers.items():
                if prompt not in err:
                    continue
                err = err.replace(prompt, b"", 1)
                if prompt in answered:
                    channel.shutdown_write()
                    stdin_open = False
                    break
                channel.sendall((answer + "\n").encode())
                answered.add(prompt)
                last_prompt = time.monotonic()
            if stdin_open and (marker in out or time.monotonic() - last_prompt > prompt_wait):
                channel.shutdown_write()
                stdin_open = False
        if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
            break
        if time.monotonic() > deadline:
            raise CommandTimeout(f"No result within {timeout}s")
        if not progressed:
            time.sleep(0.05)
    for prompt in answers:
        err = err.replace(prompt, b"")
    return Completed(channel.recv_exit_status(), bytes(out), bytes(err), out_cut, err_cut)


class JumpSession:
    """One person's SSH login to the jump server, reused by all of their /run requests."""

    def __init__(self, cfg, username, password):
        self.cfg = cfg
        self.username = username
        self._password = password
        self._lock = threading.Lock()
        self._client = connect(cfg, username, password)

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

    def exec(self, command, timeout, ccps_secret=None):
        channel = self._transport().open_session(timeout=self.cfg.connect_timeout)
        try:
            channel.exec_command(command)
            return pump(channel, self._password, timeout, 20, ccps_secret,
                        self.cfg.max_output_bytes + 65536)
        finally:
            channel.close()

    def redact(self, text):
        if self._password:
            return text.replace(self._password, "********")
        return text

    def close(self):
        with self._lock:
            if self._client:
                self._client.close()
                self._client = None
        self._password = ""
