from __future__ import annotations

import logging
import shlex
import time
from dataclasses import dataclass
from typing import List, Optional

from .config import AppConfig, ScriptConfig

log = logging.getLogger(__name__)

# Printed by the oracle shell right before ssh starts. Anything before it on stdout is
# login-profile noise (banners, oraenv), and its absence means sudo/su/ccps never got that far.
MARKER = "__DBAOPS_SSH_START__"
CCPS_FAILED = "__DBAOPS_CCPS_FAILED__"
SUDO_PROMPT = "__DBAOPS_SUDO_PASSWORD__:"
CCPS_EXIT = 97
TIMEOUT_EXIT = 124
SSH_CONNECT_EXIT = 255


class CommandTimeout(Exception):
    pass


@dataclass
class Completed:
    returncode: int
    stdout: bytes
    stderr: bytes


class Session:
    """What the runner needs from a logged-in person's jump server session."""
    username: str

    def exec(self, command: str, timeout: int) -> Completed: ...

    def redact(self, text: str) -> str: ...


@dataclass
class RunResult:
    status: str  # ok | script_failed | ccps_failed | become_failed | ssh_failed | timeout | jump_failed
    exit_code: Optional[int]
    output: str
    stderr: str
    command: str
    duration_s: float
    error: Optional[str] = None
    truncated: bool = False

    @property
    def infrastructure_error(self) -> bool:
        return self.status not in {"ok", "script_failed"}


class Runner:
    def __init__(self, config: AppConfig):
        self.config = config

    def ssh_command(self, host: str, script: ScriptConfig) -> str:
        cfg = self.config
        args = [a.replace("{hostname}", host) for a in script.args]
        if script.pass_hostname and host not in args:
            args.append(host)
        remote = shlex.join([cfg.script_path(script), *args])
        destination = f"{cfg.ssh_user}@{host}" if cfg.ssh_user else host
        return shlex.join(["ssh", *cfg.ssh_options, destination, remote])

    def oracle_shell(self, host: str, script: ScriptConfig) -> str:
        """The script the oracle login shell runs: ccps, then one ssh, then kill the agent.

        Kept to POSIX sh so it also works when oracle's login shell is ksh.
        """
        timeout = self.config.timeout_for(script)
        return "\n".join([
            "trap '[ -n \"$SSH_AGENT_PID\" ] && ssh-agent -k >/dev/null 2>&1' EXIT",
            f"{{ {self.config.ccps} ; }} </dev/null >&2 || "
            f"{{ echo {CCPS_FAILED} >&2; exit {CCPS_EXIT}; }}",
            f"echo {MARKER}; echo {MARKER} >&2",
            f"timeout {timeout} {self.ssh_command(host, script)} </dev/null",
        ])

    def argv(self, host: str, script: ScriptConfig) -> List[str]:
        """The command run in the person's jump server login, e.g. sudo -S -p ... su - oracle -c ..."""
        inner = self.oracle_shell(host, script)
        become = list(self.config.become)
        if not become:
            return ["/bin/sh", "-c", inner]
        if become[0] == "sudo":
            become[1:1] = ["-S", "-p", SUDO_PROMPT]
        return [*become, inner]

    def command(self, host: str, script: ScriptConfig) -> str:
        return shlex.join(self.argv(host, script))

    def run(self, session: Session, host: str, script: ScriptConfig) -> RunResult:
        command = self.ssh_command(host, script)
        timeout = self.config.timeout_for(script)
        started = time.monotonic()
        try:
            # The shell's own `timeout` stops ssh first; this is the backstop if sudo/su hangs.
            proc = session.exec(self.command(host, script), timeout + 30)
        except CommandTimeout:
            return RunResult("timeout", None, "", "", command, time.monotonic() - started,
                             f"No response from the jump server within {timeout + 30}s")
        except Exception as exc:  # noqa: BLE001 - reported to the caller as a 502
            log.warning("Jump server exec failed for %s: %s", session.username, exc)
            return RunResult("jump_failed", None, "", "", command, time.monotonic() - started,
                             f"Jump server session error: {exc}")
        duration = time.monotonic() - started

        limit = self.config.max_output_bytes
        truncated = len(proc.stdout) > limit
        stdout = session.redact(proc.stdout[:limit].decode("utf-8", errors="replace"))
        stderr = session.redact(proc.stderr[-limit:].decode("utf-8", errors="replace"))

        pre_out, started_ssh, out = _split(stdout)
        pre_err, _, err = _split(stderr)
        rc = proc.returncode

        if not started_ssh:
            if rc == CCPS_EXIT or CCPS_FAILED in stderr:
                error = "ccps failed (ssh-agent / ssh-add ~/.ssh/cp993) as oracle on the jump server"
                status = "ccps_failed"
            else:
                error = (f"{session.username} could not switch to oracle on the jump server "
                         f"(sudo su - oracle)")
                status = "become_failed"
            detail = (pre_err.replace(CCPS_FAILED, "").strip() or pre_out.strip())[-2000:]
            return RunResult(status, rc, "", detail, command, duration, f"{error}: {detail}")

        if rc == TIMEOUT_EXIT:
            return RunResult("timeout", rc, out, err, command, duration,
                             f"Script did not finish within {timeout}s", truncated)
        if rc == SSH_CONNECT_EXIT:
            return RunResult("ssh_failed", rc, out, err, command, duration,
                             f"ssh to {host} failed: {err.strip()[-2000:]}", truncated)
        status = "ok" if rc == 0 else "script_failed"
        return RunResult(status, rc, out, err, command, duration, None, truncated)


def _split(text: str):
    """Return (before marker, marker seen, after marker)."""
    idx = text.find(MARKER)
    if idx < 0:
        return text, False, ""
    after = text[idx + len(MARKER):]
    if after.startswith("\n"):
        after = after[1:]
    return text[:idx], True, after
