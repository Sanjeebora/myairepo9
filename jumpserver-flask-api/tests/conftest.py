import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dbaops_api.app import create_app  # noqa: E402
from dbaops_api.auth import SessionManager  # noqa: E402
from dbaops_api.runner import MARKER, Completed  # noqa: E402

SECRET = "s" * 40
API_KEY = "test-api-key"


@pytest.fixture
def config_path(tmp_path):
    data = json.loads((ROOT / "config.example.json").read_text())
    data["audit_log"] = str(tmp_path / "audit.log")
    data["llm"]["base_url"] = "http://llm.invalid/v1"
    data["llm"]["model"] = "test-model"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return str(path)


class FakeProcess:
    """Stands in for subprocess: returns what the oracle shell would print."""

    def __init__(self):
        self.calls = []
        self.returncode = 0
        self.stdout = "ORACLE_SID=PROD1 OK\n"
        self.stderr = ""
        self.reach_ssh = True
        self.preamble = "Last login: Fri\n"

    def __call__(self, argv, timeout):
        self.calls.append((argv, timeout))
        if not self.reach_ssh:
            return Completed(self.returncode, self.preamble.encode(), self.stderr.encode())
        out = f"{self.preamble}{MARKER}\n{self.stdout}"
        err = f"Agent pid 1\nIdentity added\n{MARKER}\n{self.stderr}"
        return Completed(self.returncode, out.encode(), err.encode())


@pytest.fixture
def fake():
    return FakeProcess()


@pytest.fixture
def sessions():
    return SessionManager(SECRET, 3600, API_KEY)


@pytest.fixture
def app(config_path, fake, sessions):
    return create_app(config_path, process_runner=fake, sessions=sessions,
                      analyzer=lambda llm, host, script, rc, out: f"plan for {host}/{script}")


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def headers(sessions):
    token, _ = sessions.issue("pytest")
    return {"X-Session-Token": token}
