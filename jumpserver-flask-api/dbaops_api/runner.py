import logging
import shlex
import time

log = logging.getLogger(__name__)

MARKER = "__DBAOPS_SCRIPT_START__"
CCPS_FAILED = "__DBAOPS_CCPS_FAILED__"
CCPS_PROMPT = "__DBAOPS_CCPS_SECRET__:"
CCPS_EXIT = 97
TIMEOUT_EXIT = 124
SSH_EXIT = 255


class CommandTimeout(Exception):
    pass


class Completed:
    def __init__(self, returncode, stdout, stderr, stdout_truncated=False,
                 stderr_truncated=False):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.stdout_truncated = stdout_truncated
        self.stderr_truncated = stderr_truncated


class RunResult:
    def __init__(self, status, exit_code, output, stderr, command, duration_s,
                 error=None, truncated=False):
        self.status = status
        self.exit_code = exit_code
        self.output = output
        self.stderr = stderr
        self.command = command
        self.duration_s = duration_s
        self.error = error
        self.truncated = truncated

    @property
    def infrastructure_error(self):
        return self.status not in {"ok", "script_failed"}


class Runner:
    def __init__(self, config):
        self.config = config

    def script_command(self, script):
        return self.config.script_path(script)

    def ssh_argv(self, target, script):
        argv = [
            "ssh",
            "-p", str(target.port),
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=%s" % self.config.target_connect_timeout,
            "-o", "ServerAliveInterval=30",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "UserKnownHostsFile=%s" % self.config.target_known_hosts,
        ]
        if target.host_key_alias:
            argv += ["-o", "HostKeyAlias=%s" % target.host_key_alias]
        return argv + [
            "--",
            target.connect_host,
            self.script_command(script),
        ]

    def ssh_command(self, target, script):
        return shell_join(self.ssh_argv(target, script))

    def oracle_shell(self, target, script):
        """Authenticate ccps locally as oracle, then SSH to one validated target."""
        timeout = script.timeout
        lines = [
            "trap 'unset DBAOPS_CCPS_SECRET' EXIT",
            "echo %s >&2" % CCPS_PROMPT,
            "IFS= read -r DBAOPS_CCPS_SECRET || exit %s" % CCPS_EXIT,
            "printf '%%s\\n' \"$DBAOPS_CCPS_SECRET\" | %s >&2 || "
            "{ echo %s >&2; exit %s; }" % (
                self.config.ccps_command, CCPS_FAILED, CCPS_EXIT),
            "unset DBAOPS_CCPS_SECRET",
            "echo %s; echo %s >&2" % (MARKER, MARKER),
            "timeout --signal=TERM --kill-after=10s %s %s </dev/null" % (
                timeout, self.ssh_command(target, script)),
        ]
        return "\n".join(lines)

    def argv(self, target, script):
        return ["sudo", "-n", "su", "-", "oracle", "-c",
                self.oracle_shell(target, script)]

    def command(self, target, script):
        return shell_join(self.argv(target, script))

    def run(self, executor, target, script):
        command = self.ssh_command(target, script)
        timeout = script.timeout
        started = time.monotonic()
        try:
            proc = executor.exec(
                self.argv(target, script), timeout + 30, self.config.ccps_secret)
        except CommandTimeout:
            return RunResult("timeout", None, "", "", command, time.monotonic() - started,
                             "Local operation did not finish within %ss" % (timeout + 30))
        except Exception as exc:
            log.warning("Local jump-server execution failed: %s", type(exc).__name__)
            return RunResult("jump_failed", None, "", "", command, time.monotonic() - started,
                             "Local jump-server execution failed")
        duration = time.monotonic() - started

        limit = self.config.max_output_bytes
        stdout = self._redact(proc.stdout.decode("utf-8", errors="replace"))
        stderr = self._redact(proc.stderr.decode("utf-8", errors="replace"))

        pre_out, started_script, out = _split(stdout)
        pre_err, _, err = _split(stderr)
        out, out_cut = _limit_utf8(out, limit)
        err, err_cut = _limit_utf8(err, limit)
        truncated = (proc.stdout_truncated or proc.stderr_truncated or out_cut or err_cut)
        rc = proc.returncode

        if not started_script:
            if rc == CCPS_EXIT or CCPS_FAILED in stderr:
                error = "ccps authentication failed locally as oracle"
                status = "ccps_failed"
            else:
                error = "The service could not run sudo su - oracle"
                status = "become_failed"
            detail = (pre_err.replace(CCPS_FAILED, "").strip() or pre_out.strip())[-2000:]
            return RunResult(status, rc, "", detail, command, duration,
                             "%s: %s" % (error, detail))

        if rc == TIMEOUT_EXIT:
            return RunResult("timeout", rc, out, err, command, duration,
                             "Script did not finish within %ss" % timeout, truncated)
        if rc == SSH_EXIT:
            return RunResult(
                "ssh_failed", rc, out, err, command, duration,
                "SSH to target failed: %s" % err.strip()[-2000:], truncated)
        status = "ok" if rc == 0 else "script_failed"
        return RunResult(status, rc, out, err, command, duration, None, truncated)


    def _redact(self, text):
        text = text.replace(CCPS_PROMPT, "")
        secret = self.config.ccps_secret
        if secret:
            text = text.replace(secret, "********")
        return text


def shell_join(argv):
    return " ".join(shlex.quote(value) for value in argv)


def _limit_utf8(text, limit):
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", errors="replace"), True


def _split(text):
    """Return (before marker, marker seen, after marker)."""
    idx = text.find(MARKER)
    if idx < 0:
        return text, False, ""
    after = text[idx + len(MARKER):]
    if after.startswith("\n"):
        after = after[1:]
    return text[:idx], True, after
