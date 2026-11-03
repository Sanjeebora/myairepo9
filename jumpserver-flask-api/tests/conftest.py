import json
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dbaops_api.app import create_app
from dbaops_api.runner import Completed, MARKER


class FakeExecutor:
    def __init__(self):
        self.calls = []
        self.stdout = "database OK\n"
        self.stderr = ""
        self.returncode = 0

    def factory(self, _config):
        return self

    def exec(self, argv, timeout, ccps_secret):
        self.calls.append((argv, timeout, ccps_secret))
        stdout = (MARKER + "\n" + self.stdout).encode("utf-8")
        stderr = (MARKER + "\n" + self.stderr).encode("utf-8")
        return Completed(self.returncode, stdout, stderr)


@pytest.fixture
def config_path(tmp_path):
    raw = json.loads((ROOT / "config.example.json").read_text())
    raw["audit_log"] = str(tmp_path / "audit.jsonl")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    return str(path)


@pytest.fixture
def fake():
    return FakeExecutor()


@pytest.fixture
def app(config_path, fake, monkeypatch):
    monkeypatch.setenv("DBAOPS_API_TOKEN", "api-test-token-with-at-least-32-bytes")
    monkeypatch.setenv("CCPS_SECRET", "Ccps#Secret")
    def resolver(_host, port, _family, _kind):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.20.4.8", port))]

    return create_app(
        config_path, executor_factory=fake.factory, target_resolver=resolver)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def token_header():
    return {"X-Token": "api-test-token-with-at-least-32-bytes"}


def run_request(client, headers, **changes):
    body = {
        "target_host": "dbprod01.example.internal",
        "target_port": 22,
        "script": "dba_checkout",
        "confirm": False,
    }
    body.update(changes)
    return client.post("/run", json=body, headers=headers,
                       base_url="https://gateway.example.internal")
