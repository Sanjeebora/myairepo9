from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Optional

from .config import JumpConfig


class AuthError(Exception):
    pass


class TooManyFailures(Exception):
    pass


class LoginFailed(Exception):
    """Wrong username or password."""


class JumpUnavailable(Exception):
    """The jump server could not be reached or refused the connection for another reason."""


@dataclass
class UserSession:
    token: str
    username: str
    conn: object  # JumpSession, or a fake in tests
    created: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)

    def expires_at(self, ttl: int) -> int:
        return int(self.created + ttl)

    def idle_expires_at(self, idle: int) -> int:
        return int(self.last_used + idle)


class SessionStore:
    """Live sessions in memory: token -> that person's open SSH login to the jump server.

    The password is kept only inside the open session (to answer sudo) and is never written
    to disk. Restarting the service logs everybody out. Run one gunicorn worker so every
    request sees the same sessions.
    """

    def __init__(self, cfg: JumpConfig, ttl: int,
                 connector: Callable[[JumpConfig, str, str], object]):
        self.cfg = cfg
        self.ttl = ttl
        self.idle = cfg.session_idle_seconds
        self.connector = connector
        self._sessions: Dict[str, UserSession] = {}
        self._failures: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _check_rate(self, *keys: str) -> None:
        now = time.time()
        with self._lock:
            for key in keys:
                q = self._failures[key]
                while q and now - q[0] > self.cfg.login_failure_window:
                    q.popleft()
                if len(q) >= self.cfg.max_login_failures:
                    raise TooManyFailures(
                        f"Too many failed logins. Try again in "
                        f"{int(self.cfg.login_failure_window - (now - q[0])) + 1}s.")

    def _record_failure(self, *keys: str) -> None:
        now = time.time()
        with self._lock:
            for key in keys:
                self._failures[key].append(now)

    def login(self, username: str, password: str, remote_addr: str) -> UserSession:
        keys = (f"user:{username}", f"ip:{remote_addr}")
        self._check_rate(*keys)
        try:
            conn = self.connector(self.cfg, username, password)
        except LoginFailed:
            self._record_failure(*keys)
            raise
        session = UserSession(secrets.token_urlsafe(32), username, conn)
        with self._lock:
            self._failures.pop(f"user:{username}", None)
            self._sessions[session.token] = session
        self.sweep()
        return session

    def get(self, token: Optional[str]) -> UserSession:
        if not token:
            raise AuthError("Missing session token. Log in with POST /session and send the "
                            "token in the X-Session-Token header.")
        now = time.time()
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                raise AuthError("Unknown or expired session. Log in again with POST /session.")
            if now - session.created > self.ttl or now - session.last_used > self.idle:
                del self._sessions[token]
                expired = session
            else:
                session.last_used = now
                return session
        expired.conn.close()
        raise AuthError("Session expired. Log in again with POST /session.")

    def logout(self, token: str) -> None:
        with self._lock:
            session = self._sessions.pop(token, None)
        if session:
            session.conn.close()

    def sweep(self) -> None:
        now = time.time()
        with self._lock:
            dead = [s for s in self._sessions.values()
                    if now - s.created > self.ttl or now - s.last_used > self.idle]
            for s in dead:
                del self._sessions[s.token]
        for s in dead:
            s.conn.close()

    def close_all(self) -> None:
        with self._lock:
            sessions, self._sessions = list(self._sessions.values()), {}
        for s in sessions:
            s.conn.close()
