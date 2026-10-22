from __future__ import annotations

import hmac
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .config import Settings
from .scripts import AllowedScript
from .shell import Channel, InteractiveShell, JumpShell, RunResult, ShellError

log = logging.getLogger(__name__)

_USERNAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._@\\-]{0,63}$")


class LoginError(Exception):
    def __init__(self, message: str, status: int = 502, stage: str = "", transcript: str = ""):
        super().__init__(message)
        self.status = status
        self.stage = stage
        self.transcript = transcript


class NotLoggedIn(Exception):
    pass


class Busy(Exception):
    pass


class Connection(Protocol):
    channel: Channel

    def close(self) -> None: ...


class Connector(Protocol):
    def __call__(self, settings: Settings, host: str, port: int, username: str,
                 password: str) -> Connection: ...


@dataclass
class ParamikoConnection:
    client: object
    channel: Channel

    def close(self) -> None:
        try:
            self.channel.close()
        finally:
            self.client.close()


def paramiko_connector(settings: Settings, host: str, port: int, username: str,
                       password: str) -> ParamikoConnection:
    import paramiko

    client = paramiko.SSHClient()
    known = Path(settings.known_hosts_file).expanduser() if settings.known_hosts_file else None
    if settings.host_key_policy != "insecure":
        try:
            client.load_system_host_keys()
        except OSError:
            pass
        if known and known.exists():
            client.load_host_keys(str(known))
    if settings.host_key_policy == "strict":
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        # tofu: first connection records the key in known_hosts; a changed key later fails
        # with BadHostKeyException because the old key is loaded above.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        if known and settings.host_key_policy == "tofu":
            known.parent.mkdir(parents=True, exist_ok=True)
            known.touch(exist_ok=True)
            client.load_host_keys(str(known))
    try:
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            timeout=settings.connect_timeout,
            banner_timeout=settings.connect_timeout,
            auth_timeout=settings.connect_timeout,
            look_for_keys=False,
            allow_agent=False,
        )
    except paramiko.AuthenticationException as exc:
        client.close()
        raise LoginError("The jump server rejected the username or password", 401, "ssh") from exc
    except paramiko.BadHostKeyException as exc:
        client.close()
        raise LoginError(
            f"The jump server host key changed. If this is expected, remove {host} from "
            f"{settings.known_hosts_file}", 502, "host_key") from exc
    except paramiko.SSHException as exc:
        client.close()
        raise LoginError(f"SSH error: {exc}", 502, "ssh") from exc
    except OSError as exc:
        client.close()
        raise LoginError(f"Cannot reach {host}:{port}: {exc}", 502, "connect") from exc
    transport = client.get_transport()
    if transport is not None:
        transport.set_keepalive(30)
    channel = client.invoke_shell(term="dumb", width=250, height=50)
    return ParamikoConnection(client, channel)


@dataclass
class JumpSession:
    token: str
    username: str
    jump_host: str
    connection: Connection
    shell: JumpShell
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False

    def to_dict(self, settings: Settings) -> dict:
        return {
            "username": self.username,
            "jump_host": self.jump_host,
            "created_at": _iso(self.created_at),
            "last_used": _iso(self.last_used),
            "idle_expires_at": _iso(self.last_used + settings.session_idle_timeout),
            "expires_at": _iso(self.created_at + settings.session_max_age),
            "busy": self.lock.locked(),
            "steps": list(self.shell.steps),
        }

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.shell.sh.send("exit\nexit\n")
        except Exception:  # noqa: BLE001 - best effort before closing the socket
            pass
        try:
            self.connection.close()
        except Exception:  # noqa: BLE001
            pass


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


class SessionManager:
    """Holds at most one jump-server session. A successful new login replaces the old one."""

    def __init__(self, settings: Settings, connector: Connector = paramiko_connector,
                 clock: Callable[[], float] = time.time):
        self.settings = settings
        self.connector = connector
        self.clock = clock
        self.api_key = settings.api_key or secrets.token_urlsafe(32)
        self.api_key_generated = not settings.api_key
        self._session: JumpSession | None = None
        self._lock = threading.RLock()
        self._login_lock = threading.Lock()
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None

    def login(self, username: str, password: str, jump_host: str | None = None,
              port: int | None = None) -> JumpSession:
        s = self.settings
        username = (username or "").strip()
        host = (jump_host or s.jump_host or "").strip()
        if not _USERNAME.fullmatch(username):
            raise LoginError("username is missing or has invalid characters", 400, "input")
        if not password or len(password) > 512:
            raise LoginError("password is required", 400, "input")
        if not host:
            raise LoginError("No jump server configured: set JT_JUMP_HOST or send jump_host", 400, "input")
        if not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]{1,253}", host):
            raise LoginError("jump_host is not a valid hostname", 400, "input")

        if not self._login_lock.acquire(blocking=False):
            raise LoginError("Another login is in progress", 409, "input")
        try:
            return self._login(username, password, host, port)
        finally:
            self._login_lock.release()

    def _login(self, username: str, password: str, host: str, port: int | None) -> JumpSession:
        s = self.settings
        log.info("Logging in to %s@%s:%s", username, host, port or s.jump_port)
        conn = self.connector(s, host, port or s.jump_port, username, password)
        sh = InteractiveShell(conn.channel, [password, s.ccps_password], s.max_output_bytes)
        jump = JumpShell(sh, s)
        try:
            jump.login(password)
        except ShellError as exc:
            conn.close()
            status = 401 if "rejected" in str(exc) else 502
            raise LoginError(str(exc), status, exc.stage, exc.transcript) from exc
        except Exception:
            conn.close()
            raise
        now = self.clock()
        session = JumpSession(secrets.token_urlsafe(32), username, host, conn, jump,
                              created_at=now, last_used=now)
        with self._lock:
            old, self._session = self._session, session
        if old:
            old.close()
        log.info("Session ready for %s@%s (%s)", username, host, ", ".join(jump.steps))
        return session

    def logout(self) -> bool:
        with self._lock:
            session, self._session = self._session, None
        if session:
            session.close()
            log.info("Logged out %s@%s", session.username, session.jump_host)
        return session is not None

    def check_key(self, presented: str | None) -> bool:
        if not presented:
            return False
        if hmac.compare_digest(presented, self.api_key):
            return True
        session = self._session
        return bool(session and hmac.compare_digest(presented, session.token))

    def expire_if_needed(self) -> str | None:
        with self._lock:
            session = self._session
            if not session:
                return None
            if session.closed:
                reason = "closed"
            elif session.lock.locked():
                return None
            elif self.clock() - session.last_used > self.settings.session_idle_timeout:
                reason = "idle timeout"
            elif self.clock() - session.created_at > self.settings.session_max_age:
                reason = "maximum session age"
            else:
                return None
            self._session = None
        session.close()
        log.info("Session for %s@%s ended: %s", session.username, session.jump_host, reason)
        return reason

    def current(self) -> JumpSession | None:
        self.expire_if_needed()
        return self._session

    def run(self, hosts: list[str], script: AllowedScript, timeout: int) -> tuple[list[RunResult], bool]:
        """Run one allowlisted script on each host in turn. Returns (results, session_closed)."""
        session = self.current()
        if session is None:
            raise NotLoggedIn()
        wait = self.settings.busy_wait_seconds
        if not session.lock.acquire(blocking=wait > 0, timeout=wait if wait > 0 else -1):
            raise Busy()
        results: list[RunResult] = []
        broken = False
        try:
            for host in hosts:
                if broken or session.closed:
                    results.append(_error_result(host, script, "Session closed by an earlier failure"))
                    continue
                try:
                    results.append(session.shell.run(host, script, timeout))
                except ShellError as exc:
                    log.warning("Shell failure on %s: %s", host, exc)
                    results.append(_error_result(host, script, str(exc)))
                    broken = True
                session.last_used = self.clock()
        finally:
            session.lock.release()
        if broken:
            with self._lock:
                if self._session is session:
                    self._session = None
            session.close()
        return results, broken

    def start_reaper(self, interval: float = 15) -> None:
        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    self.expire_if_needed()
                except Exception:  # noqa: BLE001 - keep the reaper alive
                    log.exception("session reaper failed")

        self._reaper = threading.Thread(target=loop, name="session-reaper", daemon=True)
        self._reaper.start()

    def shutdown(self) -> None:
        self._stop.set()
        self.logout()


def _error_result(host: str, script: AllowedScript, message: str) -> RunResult:
    return RunResult(host, script.name, "", "error", None, "", "", 0.0,
                     time.strftime("%Y-%m-%dT%H:%M:%S%z"), False, message)
