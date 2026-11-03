"""Client for the agent harness host. Standard library only; Python 3.6+.

Each person logs in with their own jump server username and password:

    export DBAOPS_URL=https://jumpserver.example.com:8443
    export DBAOPS_CA=/path/to/internal-ca.crt
    export DBAOPS_TOKEN='read from a secret manager'
    python3 agent_client.py --user jsmith dba_checkout
"""
import argparse
import getpass
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
class DbaOpsError(Exception):
    def __init__(self, status, body):
        super().__init__("HTTP %s: %s" % (status, body.get("error", body)))
        self.status = status
        self.body = body


class DbaOpsClient:
    def __init__(self, base_url, token, cafile=None, timeout=660):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._ssl = ssl.create_default_context(cafile=cafile) if cafile else None

    def _call(self, method, path, body=None):
        req = urllib.request.Request(
            self.base_url + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Token": self.token})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode())
            except ValueError:
                payload = {"error": exc.reason}
            raise DbaOpsError(exc.code, payload)

    def scripts(self):
        return self._call("GET", "/scripts")["scripts"]

    def run(self, username, password, script, confirm=False):
        return self._call("POST", "/run", {
            "username": username, "password": password, "script": script,
            "confirm": confirm,
        })


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default=os.environ.get("DBAOPS_USER", getpass.getuser()))
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("script")
    a = parser.parse_args()
    password = os.environ.get("DBAOPS_PASSWORD") or getpass.getpass(f"jump server password for {a.user}: ")
    token = os.environ.get("DBAOPS_TOKEN")
    if not token:
        parser.error("DBAOPS_TOKEN is required")
    c = DbaOpsClient(os.environ.get("DBAOPS_URL", "https://127.0.0.1:8443"), token,
                     cafile=os.environ.get("DBAOPS_CA"))
    try:
        out = c.run(a.user, password, a.script, confirm=a.confirm)
    except DbaOpsError as e:
        print(json.dumps(e.body, indent=2), file=sys.stderr)
        sys.exit(1)
    print(f"user={out.get('user')} exit_code={out['exit_code']} status={out['status']}\n")
    print(out["output"])
