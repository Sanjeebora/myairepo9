from __future__ import annotations

import logging
import shlex
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import paramiko

from .config import AppConfig, ScriptConfig, ServerConfig

log = logging.getLogger(__name__)

SSH_CONNECTION_FAILURE = 255


class CommandTimeout(Exception):
    pass


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class ScriptResult:
    server: str
    host: str
    script: str
    status: str  # ok | failed | connection_error | timeout | error
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    command: str
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class Executor(Protocol):
    def exec(self, command: str, stdin: bytes | None = None, timeout: int = 600) -> ExecResult: ...


class ParamikoExecutor:
    """A single SSH connection to the jumpserver; each exec opens a new channel on it."""

    def __init__(self, config: AppConfig):
        self._cfg = config.jumpserver
        self._client: paramiko.SSHClient | None = None

    def connect(self) -> None:
        cfg = self._cfg
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if cfg.known_hosts:
            known = Path(cfg.known_hosts).expanduser()
            if known.exists():
                client.load_host_keys(str(known))
        client.set_missing_host_key_policy(
            paramiko.AutoAddPolicy() if cfg.auto_add_host_key else paramiko.RejectPolicy()
        )
        log.info("Connecting to jumpserver %s@%s:%s", cfg.username, cfg.host, cfg.port)
        client.connect(
            hostname=cfg.host,
            port=cfg.port,
            username=cfg.username,
            key_filename=str(Path(cfg.key_file).expanduser()) if cfg.key_file else None,
            password=cfg.password,
            passphrase=cfg.key_passphrase,
            timeout=cfg.connect_timeout,
            banner_timeout=cfg.connect_timeout,
            auth_timeout=cfg.connect_timeout,
            look_for_keys=cfg.key_file is None and cfg.password is None,
            allow_agent=True,
        )
        transport = client.get_transport()
        if transport:
            transport.set_keepalive(30)
        self._client = client

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self) -> "ParamikoExecutor":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def exec(self, command: str, stdin: bytes | None = None, timeout: int = 600) -> ExecResult:
        if not self._client:
            raise RuntimeError("Not connected")
        transport = self._client.get_transport()
        if transport is None or not transport.is_active():
            raise RuntimeError("Jumpserver connection is not active")
        channel = transport.open_session()
        try:
            channel.exec_command(command)
            if stdin:
                channel.sendall(stdin)
            channel.shutdown_write()

            out, err = bytearray(), bytearray()
            deadline = time.monotonic() + timeout
            while True:
                progressed = False
                while channel.recv_ready():
                    out += channel.recv(65536)
                    progressed = True
                while channel.recv_stderr_ready():
                    err += channel.recv_stderr(65536)
                    progressed = True
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                if time.monotonic() > deadline:
                    raise CommandTimeout(f"Command exceeded {timeout}s")
                if not progressed:
                    time.sleep(0.05)
            return ExecResult(
                exit_code=channel.recv_exit_status(),
                stdout=out.decode("utf-8", errors="replace"),
                stderr=err.decode("utf-8", errors="replace"),
            )
        finally:
            channel.close()


class JumpServerRunner:
    def __init__(self, config: AppConfig, executor: Executor):
        self.config = config
        self.executor = executor

    def wrap(self, command: str) -> str:
        if self.config.jumpserver.login_shell:
            return f"bash -lc {shlex.quote(command)}"
        return command

    def run_ccps(self) -> ExecResult:
        ccps = self.config.ccps
        log.info("Running '%s' on jumpserver", ccps.command)
        result = self.executor.exec(self.wrap(ccps.command), timeout=ccps.timeout)
        if result.exit_code != 0:
            raise RuntimeError(
                f"'{ccps.command}' failed with exit code {result.exit_code}: "
                f"{(result.stderr or result.stdout).strip()[:500]}"
            )
        return result

    def build_command(self, server: ServerConfig, script: ScriptConfig) -> str:
        user = server.user or self.config.target_ssh.user
        destination = f"{user}@{server.host}" if user else server.host
        if script.remote_path:
            remote = shlex.join([script.remote_path, *script.args])
        else:
            remote = shlex.join(["bash", "-s", "--", *script.args])
        command = shlex.join(["ssh", *self.config.target_ssh.options, destination, remote])
        if self.config.ccps.mode == "per_command":
            command = f"{{ {self.config.ccps.command}; }} 1>&2 && {command}"
        return self.wrap(command)

    def run_script(self, server: ServerConfig, script: ScriptConfig) -> ScriptResult:
        command = self.build_command(server, script)
        stdin = Path(script.local_file).read_bytes() if script.local_file else None
        started = time.monotonic()
        log.info("[%s] running %s", server.name, script.name)
        try:
            res = self.executor.exec(command, stdin=stdin, timeout=script.timeout)
        except CommandTimeout as exc:
            return ScriptResult(server.name, server.host, script.name, "timeout", None, "", "",
                                time.monotonic() - started, command, str(exc))
        except Exception as exc:  # noqa: BLE001 - one bad server must not stop the batch
            return ScriptResult(server.name, server.host, script.name, "error", None, "", "",
                                time.monotonic() - started, command, f"{type(exc).__name__}: {exc}")

        if res.exit_code == SSH_CONNECTION_FAILURE:
            status = "connection_error"
        elif res.exit_code == 0:
            status = "ok"
        else:
            status = "failed"
        log.info("[%s] %s finished: %s (exit %s)", server.name, script.name, status, res.exit_code)
        return ScriptResult(server.name, server.host, script.name, status, res.exit_code,
                            res.stdout, res.stderr, time.monotonic() - started, command)

    def run_many(self, servers: list[ServerConfig], scripts: list[ScriptConfig]) -> list[ScriptResult]:
        jobs = [(srv, scr) for srv in servers for scr in scripts]
        workers = max(1, min(self.config.max_parallel, len(jobs)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(lambda job: self.run_script(*job), jobs))
