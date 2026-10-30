from __future__ import annotations

import hmac
import os
import secrets
import time
from typing import Optional, Tuple

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SALT = "dbaops-session-v1"


class AuthError(Exception):
    pass


class SessionManager:
    """Signed, expiring session tokens. Nothing is stored server-side, so tokens keep
    working across gunicorn workers and restarts. Rotate DBAOPS_SECRET_KEY to revoke all."""

    def __init__(self, secret_key: str, ttl_seconds: int, api_key: Optional[str] = None):
        if not secret_key or len(secret_key) < 32:
            raise ValueError("DBAOPS_SECRET_KEY must be set to at least 32 characters")
        self.ttl = ttl_seconds
        self.api_key = api_key
        self._serializer = URLSafeTimedSerializer(secret_key, salt=SALT)

    @classmethod
    def from_env(cls, ttl_seconds: int) -> "SessionManager":
        return cls(os.environ.get("DBAOPS_SECRET_KEY", ""), ttl_seconds,
                   os.environ.get("DBAOPS_API_KEY") or None)

    def issue(self, client: str = "agent") -> Tuple[str, int]:
        token = self._serializer.dumps({"sub": client[:64], "jti": secrets.token_urlsafe(8)})
        return token, int(time.time()) + self.ttl

    def verify(self, token: Optional[str]) -> str:
        if not token:
            raise AuthError("Missing session token. Send it in the X-Session-Token header.")
        try:
            data = self._serializer.loads(token, max_age=self.ttl)
        except SignatureExpired:
            raise AuthError("Session expired. Request a new token from POST /session.")
        except BadSignature:
            raise AuthError("Invalid session token.")
        return str(data.get("sub", "agent"))

    def check_api_key(self, presented: Optional[str]) -> bool:
        if not self.api_key or not presented:
            return False
        return hmac.compare_digest(presented.encode(), self.api_key.encode())
