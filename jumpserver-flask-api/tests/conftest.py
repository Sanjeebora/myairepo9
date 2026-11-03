import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dbaops_api.app import create_app
from dbaops_api.jumphost import LoginFailed
from dbaops_api.runner import Completed, MARKER


class FakeSession:
    def __init__(self, owner, username, password):
        self.owner = owner
        self.username = username
        self.password = password
        self.closed = False

    def exec(self, command, timeout, ccps_secret=None):
        self.owner.calls.append((self.username, command, timeout, ccps_secret))
        stdout = (MARKER + "\n" + self.owner.stdout).encode("utf-8")
        stderr = (MARKER + "\n" + self.owner.stderr).encode("utf-8")
        return Completed(self.owner.returncode, stdout, stderr)

    def redact(self, text):
        return text.replace(self.password, "********")

    def close(self):
        self.closed = True


class FakeConnector:
    def __init__(self):
        self.calls = []
        self.sessions = []
        self.stdout = "database OK\n"
        self.stderr = ""
        self.returncode = 0

    def connect(self, config, username, password):
        if password != "Login#Secret":
            raise LoginFailed("rejected")
        session = FakeSession(self, username, password)
        self.sessions.append(session)
        return session


@pytest.fixture
def config_path(tmp_path):
    raw = json.loads((ROOT / "config.example.json").read_text())
    raw["audit_log"] = str(tmp_path / "audit.jsonl")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    return str(path)


@pytest.fixture
def fake():
    return FakeConnector()


@pytest.fixture
def app(config_path, fake, monkeypatch):
    monkeypatch.setenv("DBAOPS_API_TOKEN", "api-test-token-with-at-least-32-bytes")
    monkeypatch.setenv("CCPS_SECRET", "Ccps#Secret")
    return create_app(config_path, connector=fake.connect)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def token_header():
    return {"X-Token": "api-test-token-with-at-least-32-bytes"}


def run_request(client, headers, **changes):
    body = {
        "username": "jsmith",
        "password": "Login#Secret",
        "script": "dba_checkout",
        "confirm": False,
    }
    body.update(changes)
    return client.post("/run", json=body, headers=headers,
                       base_url="https://gateway.example.internal")
