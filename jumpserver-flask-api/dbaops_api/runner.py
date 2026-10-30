from __future__ import annotations

import logging
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from .config import AppConfig, ScriptConfig

log = logging.getLogger(__name__)

# Printed by the oracle shell right before ssh starts. Anything before it on stdout is
# login-profile noise (banners, oraenv), and its absence means sudo/su/ccps never got that far.
MARKER = "__DBAOPS_SSH_START__"
CCPS_FAILED = "__DBAOPS_CCPS_FAILED__"
CCPS_EXIT = 97
TIMEOUT_EXIT = 124
SSH_CONNECT_EXIT = 255


@dataclass
class Completed:
    returncode: int
    stdout: bytes
    stderr: bytes


# Injected in tests; the default runs the real command on the jump server.
ProcessRunner = Callable[[List[str], int], Completed]


def run_process(argv: List[str], timeout: int) -> Completed:
    proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout)
    return Completed(proc.returncode, proc.stdout, proc.stderr)


@dataclass
class RunResult:
    status: str  # ok | script_failed | ccps_failed | become_failed | ssh_failed | timeout
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
    def __init__(self, config: AppConfig, process_runner: Optional[ProcessRunner] = None):
        self.config = config
        self.process_runner = process_runner or run_process

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
        inner = self.oracle_shell(host, script)
        if self.config.become:
            return [*self.config.become, inner]
        return ["/bin/sh", "-c", inner]

    def run(self, host: str, script: ScriptConfig) -> RunResult:
        command = self.ssh_command(host, script)
        argv = self.argv(host, script)
        timeout = self.config.timeout_for(script)
        started = time.monotonic()
        try:
            # The shell's own `timeout` stops ssh first; this is the backstop if sudo/su hangs.
            proc = self.process_runner(argv, timeout + 30)
        except subprocess.TimeoutExpired:
            return RunResult("timeout", None, "", "", command, time.monotonic() - started,
                             f"No response from the jump server shell within {timeout + 30}s")
        except OSError as exc:
            return RunResult("become_failed", None, "", "", command, time.monotonic() - started,
                             f"Could not start {argv[0]}: {exc}")
        duration = time.monotonic() - started

        limit = self.config.max_output_bytes
        truncated = len(proc.stdout) > limit
        stdout = proc.stdout[:limit].decode("utf-8", errors="replace")
        stderr = proc.stderr[-limit:].decode("utf-8", errors="replace")

        pre_out, started_ssh, out = _split(stdout)
        pre_err, _, err = _split(stderr)
        rc = proc.returncode

        if not started_ssh:
            if rc == CCPS_EXIT or CCPS_FAILED in stderr:
                error = "ccps failed (ssh-agent / ssh-add ~/.ssh/cp993) as oracle on the jump server"
                status = "ccps_failed"
            else:
                error = "Could not switch to oracle on the jump server (sudo su - oracle)"
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
