"""Client for the agent harness host. Standard library only; Python 3.8+.

    export DBAOPS_URL=http://jumpserver:8080
    export DBAOPS_API_KEY=...            # or DBAOPS_TOKEN=... if you mint tokens on the jump server
    python3 agent_client.py dbprod01 dba_checkout
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, Optional


class DbaOpsError(Exception):
    def __init__(self, status: int, body: Dict[str, Any]):
        super().__init__(f"HTTP {status}: {body.get('error', body)}")
        self.status = status
        self.body = body


class DbaOpsClient:
    def __init__(self, base_url: str, api_key: Optional[str] = None,
                 token: Optional[str] = None, timeout: int = 1000):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.token = token
        self.timeout = timeout

    def _call(self, method: str, path: str, body: Optional[dict] = None,
              headers: Optional[dict] = None) -> Dict[str, Any]:
        req = urllib.request.Request(
            self.base_url + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode())
            except ValueError:
                payload = {"error": exc.reason}
            raise DbaOpsError(exc.code, payload) from None

    def login(self, client: str = "agent-harness") -> str:
        if not self.api_key:
            raise RuntimeError("Session expired and no DBAOPS_API_KEY is set to renew it")
        data = self._call("POST", "/session", {"client": client}, {"X-API-Key": self.api_key})
        self.token = data["token"]
        return self.token

    def _authed(self, method: str, path: str, body: Optional[dict] = None) -> Dict[str, Any]:
        if not self.token:
            self.login()
        try:
            return self._call(method, path, body, {"X-Session-Token": self.token})
        except DbaOpsError as exc:
            if exc.status != 401 or not self.api_key:
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
    if len(sys.argv) < 3:
        sys.exit("usage: agent_client.py HOST SCRIPT [--confirm]")
    c = DbaOpsClient(os.environ.get("DBAOPS_URL", "http://127.0.0.1:8080"),
                     api_key=os.environ.get("DBAOPS_API_KEY"), token=os.environ.get("DBAOPS_TOKEN"))
    try:
        out = c.run(sys.argv[1], sys.argv[2], confirm="--confirm" in sys.argv)
    except DbaOpsError as e:
        print(json.dumps(e.body, indent=2), file=sys.stderr)
        sys.exit(1)
    print(f"exit_code={out['exit_code']} status={out['status']}\n")
    print(out["output"])
