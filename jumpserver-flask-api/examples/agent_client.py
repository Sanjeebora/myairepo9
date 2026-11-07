"""Client for the agent harness host. Standard library only; Python 3.8+.

Each person logs in with their own jump server username and password:

    export DBAOPS_URL=https://jumpserver.example.com:8443
    export DBAOPS_CA=/path/to/jumpserver-ca.crt        # if the certificate is self-signed
    # Inject DBAOPS_XTOKEN from the harness secret store (do not paste it into source or flags).
    python3 agent_client.py --user jsmith dbprod01 dba_checkout

Plain HTTP is rejected by default. It may be explicitly enabled only when the private route is
already encrypted (for example WireGuard) by setting DBAOPS_ALLOW_HTTP=1.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional


class DbaOpsError(Exception):
    def __init__(self, status: int, body: Dict[str, Any]):
        super().__init__(f"HTTP {status}: {body.get('error', body)}")
        self.status = status
        self.body = body


class DbaOpsClient:
    def __init__(self, base_url: str, username: str, password: Optional[str] = None,
                 token: Optional[str] = None, cafile: Optional[str] = None, timeout: int = 1000,
                 xtoken: Optional[str] = None, allow_http: bool = False):
        self.base_url = base_url.rstrip("/")
        scheme = urllib.parse.urlsplit(self.base_url).scheme.lower()
        if scheme not in {"http", "https"}:
            raise ValueError("DBA Ops URL must use http or https")
        if scheme == "http" and not allow_http:
            raise ValueError(
                "Plain HTTP can expose credentials and tokens. Use HTTPS, or set allow_http=True "
                "only for an encrypted private-network route.")
        if not xtoken:
            raise ValueError("An xToken must be injected at runtime")
        self.username = username
        self._password = password
        self.token = token
        self._xtoken = xtoken
        self.timeout = timeout
        self._ssl = ssl.create_default_context(cafile=cafile) if cafile else None

    def _call(self, method: str, path: str, body: Optional[dict] = None,
              headers: Optional[dict] = None) -> Dict[str, Any]:
        req = urllib.request.Request(
            self.base_url + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Token": self._xtoken,
                     **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode())
            except ValueError:
                payload = {"error": exc.reason}
            raise DbaOpsError(exc.code, payload) from None

    def login(self) -> str:
        if not self._password:
            raise RuntimeError("Session expired; a password is needed to log in again")
        data = self._call("POST", "/session",
                          {"username": self.username, "password": self._password})
        self.token = data["token"]
        return self.token

    def logout(self) -> None:
        if self.token:
            try:
                self._call("DELETE", "/session", headers={"X-Session-Token": self.token})
            finally:
                self.token = None

    def _authed(self, method: str, path: str, body: Optional[dict] = None) -> Dict[str, Any]:
        if not self.token:
            self.login()
        try:
            return self._call(method, path, body, {"X-Session-Token": self.token})
        except DbaOpsError as exc:
            if exc.status != 401 or not self._password:
                raise
            self.login()
            return self._call(method, path, body, {"X-Session-Token": self.token})

    def scripts(self) -> list:
        return self._authed("GET", "/scripts")["scripts"]

    def tools(self) -> list:
        """OpenAI-style tool definitions to pass to your LLM."""
        return self._authed("GET", "/tools")["tools"]

    def run(self, host: str, script: str, confirm: bool = False, analyze: bool = False) -> dict:
        """Returns the response body. Raises DbaOpsError on 400/401/409/429/502."""
        return self._authed("POST", "/run", {"host": host, "script": script,
                                             "confirm": confirm, "analyze": analyze})


def handle_tool_call(client: DbaOpsClient, arguments_json: str) -> str:
    """Call this when your LLM asks for the run_dba_script tool; return the string to the LLM."""
    args = json.loads(arguments_json)
    try:
        result = client.run(args["host"], args["script"], confirm=bool(args.get("confirm")))
    except DbaOpsError as exc:
        return json.dumps({"error": str(exc), **exc.body})
    return json.dumps({k: result.get(k) for k in ("host", "script", "exit_code", "status", "output")})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default=os.environ.get("DBAOPS_USER", getpass.getuser()))
    parser.add_argument("--confirm", action="store_true",
                        help="confirm a state-changing allow-listed script")
    parser.add_argument("host")
    parser.add_argument("script")
    a = parser.parse_args()
    password = os.environ.get("DBAOPS_PASSWORD") or getpass.getpass(f"jump server password for {a.user}: ")
    c = DbaOpsClient(
        os.environ.get("DBAOPS_URL", "https://127.0.0.1:8443"), a.user, password,
        cafile=os.environ.get("DBAOPS_CA"), xtoken=os.environ.get("DBAOPS_XTOKEN"),
        allow_http=os.environ.get("DBAOPS_ALLOW_HTTP") == "1")
    try:
        out = c.run(a.host, a.script, confirm=a.confirm)
    except DbaOpsError as e:
        print(json.dumps(e.body, indent=2), file=sys.stderr)
        sys.exit(1)
    finally:
        try:
            c.logout()
        except Exception:  # noqa: BLE001
            pass
    print(json.dumps(out, indent=2))
