"""Expect-style driver for the interactive jump-server shell.

Every step that needs a result prints a marker with a per-call nonce through
`printf '__JT_X_%s__' <nonce>`. The terminal echo of that command line shows `%s`,
not the nonce, so only the real output can match.
"""
from __future__ import annotations

import codecs
import logging
import re
import secrets
import shlex
import time
from dataclasses import asdict, dataclass
from typing import Protocol

from .config import Settings
from .scripts import AllowedScript

log = logging.getLogger(__name__)

SSH_CONNECTION_FAILURE = 255
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[@-Z\\-_]")
QUIET = -1


class Channel(Protocol):
    def send(self, data: bytes) -> int: ...
    def recv_ready(self) -> bool: ...
    def recv(self, nbytes: int) -> bytes: ...
    def exit_status_ready(self) -> bool: ...
    def close(self) -> None: ...


class ShellError(RuntimeError):
    def __init__(self, message: str, transcript: str = "", stage: str = ""):
        super().__init__(message)
        self.transcript = transcript
        self.stage = stage


class ShellTimeout(ShellError):
    pass


@dataclass
class RunResult:
    host: str
    script: str
    command: str
    status: str  # ok | failed | connection_error | timeout | error
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    started_at: str
    truncated: bool = False
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def clean(text: str) -> str:
    return _ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "")


class InteractiveShell:
    def __init__(self, channel: Channel, secrets_to_redact: list[str] | None = None,
                 max_buffer: int = 2_000_000):
        self.channel = channel
        self.secrets = [s for s in (secrets_to_redact or []) if s]
        self.max_buffer = max_buffer
        self.buffer = ""
        self.mark = 0
        self.truncated = False
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._last_data = time.monotonic()

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "[redacted]")
        return text

    def transcript(self, tail: int = 4000) -> str:
        return self.redact(clean(self.buffer[-tail:]))

    @property
    def closed(self) -> bool:
        return bool(getattr(self.channel, "closed", False)) or self.channel.exit_status_ready()

    def pump(self) -> bool:
        got = False
        while self.channel.recv_ready():
            data = self.channel.recv(65536)
            if not data:
                break
            self.buffer += self._decoder.decode(data)
            got = True
        if got:
            self._last_data = time.monotonic()
            self._cap()
        return got

    def _cap(self) -> None:
        pending = len(self.buffer) - self.mark
        if pending <= self.max_buffer:
            return
        half = self.max_buffer // 2
        head = self.buffer[: self.mark + half]
        self.buffer = head + "\n...[output truncated]...\n" + self.buffer[-half:]
        self.truncated = True

    def reset(self) -> None:
        """Drop everything already read so later searches start fresh."""
        self.pump()
        self.buffer = ""
        self.mark = 0
        self.truncated = False

    def send(self, text: str) -> None:
        data = text.encode("utf-8")
        while data:
            sent = self.channel.send(data)
            if sent <= 0:
                raise ShellError("SSH channel closed while sending", self.transcript())
            data = data[sent:]

    def send_line(self, text: str) -> None:
        self.send(text + "\n")

    def expect(self, patterns: list[re.Pattern], timeout: float, quiet: float | None = None,
               stage: str = "") -> tuple[int, re.Match | None]:
        """Wait until one pattern matches output after `mark`, or (if `quiet`) output stalls.

        Returns (pattern index, match) and moves `mark` past the match, or (QUIET, None).
        """
        start = time.monotonic()
        deadline = start + timeout
        while True:
            self.pump()
            pending = self.buffer[self.mark:]
            best: tuple[int, re.Match] | None = None
            for i, pat in enumerate(patterns):
                m = pat.search(pending)
                if m and (best is None or m.start() < best[1].start()):
                    best = (i, m)
            if best:
                self.mark += best[1].end()
                return best
            now = time.monotonic()
            if quiet is not None and now - max(self._last_data, start) >= quiet:
                return QUIET, None
            if self.closed and not self.channel.recv_ready():
                raise ShellError(f"SSH session closed while waiting for {stage or 'output'}",
                                 self.transcript(), stage)
            if now >= deadline:
                raise ShellTimeout(f"Timed out after {timeout:.0f}s waiting for {stage or 'output'}",
                                   self.transcript(), stage)
            time.sleep(0.03)


def _nonce() -> str:
    return secrets.token_hex(6)


class JumpShell:
    """Drives login -> become oracle -> ccps -> `ssh <host> <script>` on one interactive shell."""

    def __init__(self, shell: InteractiveShell, settings: Settings):
        self.sh = shell
        self.cfg = settings
        self.sudo_prompt = re.compile(settings.sudo_prompt_regex)
        self.ccps_prompt = re.compile(settings.ccps_prompt_regex)
        self.shell_prompt = re.compile(settings.shell_prompt_regex)
        self.auth_failure = re.compile(settings.auth_failure_regex)
        self.steps: list[str] = []

    def sync(self, timeout: float = 30, stage: str = "shell") -> None:
        n = _nonce()
        self.sh.send_line(f"printf '__JT_SYNC_%s__\\n' {n}")
        self.sh.expect([re.compile(rf"__JT_SYNC_{n}__\r?\n")], timeout, stage=stage)
        # Swallow the prompt printed after the marker, or a later prompt-wait would match it.
        self.sh.expect([self.shell_prompt], timeout, quiet=0.5, stage=stage)

    def query(self, expr: str, timeout: float = 30, stage: str = "shell") -> str:
        """Print `expr` (shell syntax, e.g. "$(id -un)") behind a marker and return it."""
        n = _nonce()
        self.sh.send_line(f"printf '__JT_Q_%s__:%s:__\\n' {n} \"{expr}\"")
        _, m = self.sh.expect([re.compile(rf"__JT_Q_{n}__:(.*?):__\r?\n")], timeout, stage=stage)
        return m.group(1)

    def _answer(self, secret: str) -> None:
        # Some prompts switch echo off (flushing typed-ahead input) just after printing.
        time.sleep(self.cfg.prompt_reply_delay)
        self.sh.pump()
        self.sh.send_line(secret)

    def start(self) -> None:
        cfg = self.cfg
        self.sh.expect([self.shell_prompt], cfg.login_timeout, quiet=cfg.banner_quiet_seconds,
                       stage="the jump server shell prompt")
        self.sync(cfg.login_timeout, "the jump server shell")
        self.steps.append("shell ready")

    def become(self, password: str) -> None:
        cfg = self.cfg
        if not cfg.become_command:
            return
        stage = f"'{cfg.become_command}'"
        self.sh.mark = len(self.sh.buffer)
        self.sh.send_line(cfg.become_command)
        sent = 0
        while True:
            # The quiet fallback is only for prompts no regex recognises. It must be long: a
            # line typed before a slow sudo/ccps prompt appears is read as the password.
            idx, _ = self.sh.expect([self.sudo_prompt, self.auth_failure, self.shell_prompt],
                                    cfg.login_timeout, quiet=cfg.quiet_seconds, stage=stage)
            if idx == 0:
                if sent:
                    raise ShellError("sudo rejected your password", self.sh.transcript(), stage)
                self._answer(password)
                sent += 1
                self.steps.append("sudo password sent")
                continue
            if idx == 1:
                raise ShellError("sudo rejected your password", self.sh.transcript(), stage)
            break
        user = self.query("$(id -un)", cfg.login_timeout, "the account after become")
        if cfg.become_user and user != cfg.become_user:
            raise ShellError(f"Expected to be {cfg.become_user} after {stage}, but the shell is {user!r}. "
                             "Check that your account may run it.", self.sh.transcript(), stage)
        self.steps.append(f"became {user}")

    def prepare(self) -> None:
        # Echo off keeps command lines out of captured output; markers do not rely on it.
        self.sh.send_line("stty -echo 2>/dev/null; unset HISTFILE")
        self.sync(self.cfg.login_timeout, "terminal setup")

    def ccps(self) -> None:
        cfg = self.cfg
        if not cfg.ccps_command:
            return
        stage = f"'{cfg.ccps_command}'"
        self.sh.mark = len(self.sh.buffer)
        self.sh.send_line(cfg.ccps_command)
        prompts = 0
        while True:
            idx, _ = self.sh.expect([self.ccps_prompt, self.auth_failure, self.shell_prompt],
                                    cfg.login_timeout, quiet=cfg.quiet_seconds, stage=stage)
            if idx == 0:
                prompts += 1
                if prompts > cfg.ccps_max_prompts:
                    raise ShellError(f"{stage} kept asking for a password; check JT_CCPS_PASSWORD",
                                     self.sh.transcript(), stage)
                self._answer(cfg.ccps_password)
                self.steps.append("ccps password sent")
                continue
            if idx == 1:
                raise ShellError(f"{stage} rejected the password; check JT_CCPS_PASSWORD",
                                 self.sh.transcript(), stage)
            break
        rc = self.query("$?", cfg.login_timeout, f"{stage} to finish")
        if cfg.ccps_require_prompt and prompts == 0:
            raise ShellError(f"{stage} never asked for a password", self.sh.transcript(), stage)
        if rc != "0" and not cfg.ccps_ignore_exit_code:
            raise ShellError(f"{stage} exited with code {rc}", self.sh.transcript(), stage)
        self.steps.append(f"ccps done (exit {rc})")

    def login(self, password: str) -> None:
        self.start()
        self.become(password)
        self.prepare()
        self.ccps()
        self.sh.reset()

    def build_command(self, host: str, script: AllowedScript) -> str:
        options = shlex.split(self.cfg.target_ssh_options)
        return shlex.join(["ssh", *options, "--", host, shlex.join(script.argv)])

    def run(self, host: str, script: AllowedScript, timeout: int) -> RunResult:
        n = _nonce()
        command = self.build_command(host, script)
        line = (
            f"__jt_e=$(mktemp 2>/dev/null || echo /tmp/.jt_err_$$_{n}); "
            f"printf '__JT_OUT_%s__\\n' {n}; "
            f"{command} 2>\"$__jt_e\"; __jt_rc=$?; "
            f"printf '\\n__JT_ERR_%s__\\n' {n}; cat \"$__jt_e\" 2>/dev/null; rm -f \"$__jt_e\"; "
            f"printf '\\n__JT_END_%s__:%s\\n' {n} \"$__jt_rc\""
        )
        started_wall = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        started = time.monotonic()
        self.sh.reset()
        self.sh.send_line(line)
        try:
            self.sh.expect([re.compile(rf"__JT_OUT_{n}__\r?\n")], 60, stage="the command to start")
            body_start = self.sh.mark
            _, m = self.sh.expect([re.compile(rf"__JT_END_{n}__:(\d+)\r?\n")], timeout,
                                  stage=f"{script.name} on {host}")
        except ShellTimeout as exc:
            partial = self.sh.redact(clean(self.sh.buffer[self.sh.mark:]))
            self._interrupt()
            return RunResult(host, script.name, command, "timeout", None, partial, "",
                             round(time.monotonic() - started, 3), started_wall, self.sh.truncated, str(exc))

        body = self.sh.buffer[body_start: self.sh.mark - len(m.group(0))]
        rc = int(m.group(1))
        err_marker = re.search(rf"\r?\n__JT_ERR_{n}__\r?\n", body)
        if err_marker:
            out, err = body[: err_marker.start()], body[err_marker.end():]
        else:
            out, err = body, ""
        err = re.sub(r"\r?\n$", "", err)
        stdout = self.sh.redact(clean(out))
        stderr = self.sh.redact(clean(err))
        if rc == 0:
            status = "ok"
        elif rc == SSH_CONNECTION_FAILURE:
            status = "connection_error"
        else:
            status = "failed"
        return RunResult(host, script.name, command, status, rc, stdout, stderr,
                         round(time.monotonic() - started, 3), started_wall, self.sh.truncated)

    def _interrupt(self) -> None:
        """Ctrl-C the stuck command and resync; raise if the shell cannot be recovered."""
        for _ in range(2):
            self.sh.send("\x03")
            time.sleep(0.3)
        try:
            self.sync(20, "the shell after Ctrl-C")
        except ShellError as exc:
            raise ShellError("The jump server shell did not recover after a timeout; log in again",
                             exc.transcript, "recover") from exc
