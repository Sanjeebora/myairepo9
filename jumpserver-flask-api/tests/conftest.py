import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dbaops_api.app import create_app  # noqa: E402
from dbaops_api.runner import MARKER, Completed  # noqa: E402
from dbaops_api.sessions import LoginFailed  # noqa: E402

PASSWORDS = {"jsmith": "Secret#123", "akumar": "Other#456"}


@pytest.fixture
def config_path(tmp_path):
    data = json.loads((ROOT / "config.example.json").read_text())
    data["audit_log"] = str(tmp_path / "audit.log")
    data["llm"]["base_url"] = "http://llm.invalid/v1"
    data["llm"]["model"] = "test-model"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return str(path)


class FakeJump:
    """Stands in for one person's SSH login: returns what the oracle shell would print."""

    def __init__(self, username, password, script):
        self.username = username
        self.password = password
        self.script = script
        self.closed = False

    def exec(self, command, timeout):
        s = self.script
        s.calls.append((self.username, command, timeout))
        if not s.reach_ssh:
            return Completed(s.returncode, s.preamble.encode(), s.stderr.encode())
        out = f"{s.preamble}{MARKER}\n{s.stdout}"
        err = f"Agent pid 1\nIdentity added\n{MARKER}\n{s.stderr}"
        return Completed(s.returncode, out.encode(), err.encode())

    def redact(self, text):
        return text.replace(self.password, "********")

    def close(self):
        self.closed = True


class FakeJumpServer:
    def __init__(self):
        self.calls = []
        self.sessions = []
        self.returncode = 0
        self.stdout = "ORACLE_SID=PROD1 OK\n"
        self.stderr = ""
        self.reach_ssh = True
        self.preamble = "Last login: Fri\n"

    def connect(self, cfg, username, password):
        if PASSWORDS.get(username) != password:
            raise LoginFailed("bad password")
        conn = FakeJump(username, password, self)
        self.sessions.append(conn)
        return conn


@pytest.fixture
def fake():
    return FakeJumpServer()


@pytest.fixture
def app(config_path, fake):
    return create_app(config_path, connector=fake.connect,
                      analyzer=lambda llm, host, script, rc, out: f"plan for {host}/{script}")


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, username="jsmith", password=None):
    return client.post("/session", json={"username": username,
                                         "password": password or PASSWORDS[username]})


@pytest.fixture
def headers(client):
    resp = login(client)
    assert resp.status_code == 200, resp.get_json()
    return {"X-Session-Token": resp.get_json()["token"]}
