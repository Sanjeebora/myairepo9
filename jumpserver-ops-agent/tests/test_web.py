import json
import textwrap
from pathlib import Path

import pytest

from ops_agent.cli import main
from ops_agent.ssh_runner import ExecResult
from ops_agent.web import create_app

CONFIG = """
jumpserver:
  host: jump.local
  username: ops
ccps:
  command: ccps
  mode: once
target_ssh:
  user: oracle
servers:
  - name: db01
    host: db01.internal
    tags: [prod]
  - name: db02
    host: db02.internal
    tags: [uat]
scripts:
  dba_checkout:
    description: Daily DBA health checkout
    remote_path: /home/oracle/scripts/dba_checkout.sh
  os_checkout:
    remote_path: /home/oracle/scripts/os_checkout.sh
llm:
  model: test-model
reports_dir: {reports}
"""

PLAN = {
    "overall_status": "WARNING",
    "summary": "USERS tablespace is 97 percent full.",
    "servers": [{"name": "db01", "status": "WARNING", "findings": [
        {"severity": "HIGH", "title": "USERS 97%", "evidence": "USERS 97", "impact": "Can fill up"}]}],
    "action_plan": [{"priority": 1, "title": "Free space in USERS", "servers": ["db01"],
                     "reason": "97 percent", "steps": ["Check large segments"],
                     "suggested_commands": ["df -h"], "risk": "low", "owner": "DBA"}],
    "needs_human_review": [],
}


class FakeExecutor:
    def __init__(self, *_args, **_kwargs):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def exec(self, command, stdin=None, timeout=600):
        self.calls.append(command)
        if "ssh" not in command:
            return ExecResult(0, "ccps ok", "")
        return ExecResult(0, "password=hunter2\nUSERS 97", "")


class FakeModel:
    def __init__(self, *_args, **_kwargs):
        self.prompts = []

    def complete(self, system, user):
        self.prompts.append(user)
        return json.dumps(PLAN)


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(CONFIG).format(reports=tmp_path / "reports"))
    return path


@pytest.fixture
def app(config_path: Path):
    application = create_app(config_path, executor_factory=lambda cfg: FakeExecutor(),
                             model_factory=lambda cfg: FakeModel())
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def _wait(app, job_id: str):
    job = app.extensions["jobs"][job_id]
    job.thread.join(5)
    assert job.status in {"done", "error"}
    return job


def test_home_lists_allowlisted_scripts_and_has_no_command_box(client):
    page = client.get("/")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "dba_checkout" in body
    assert "db01" in body
    assert 'name="command"' not in body
    assert "Dry run" in body


def test_missing_config_explains_windows_setup(tmp_path: Path):
    application = create_app(tmp_path / "missing.yaml")
    page = application.test_client().get("/")
    assert page.status_code == 200
    assert b"Copy-Item config.example.yaml config.yaml" in page.data


def test_rejects_script_outside_the_allowlist(client):
    page = client.post("/runs", data={"script": "rm_rf"})
    assert page.status_code == 400
    assert b"allowlist" in page.data


def test_requires_a_script(client):
    page = client.post("/runs", data={})
    assert page.status_code == 400
    assert b"at least one script" in page.data


def test_dry_run_shows_commands_and_does_not_connect(app, client):
    response = client.post("/runs", data={"script": "dba_checkout", "server": "db01", "dry_run": "1"})
    assert response.status_code == 302
    job_id = response.location.rstrip("/").split("/")[-1]
    job = _wait(app, job_id)
    assert job.status == "done"
    assert job.outcome.dry_run
    text = "\n".join(job.outcome.commands)
    assert "ccps" in text
    assert "oracle@db01.internal" in text
    assert "dba_checkout.sh" in text
    page = client.get(f"/runs/{job_id}")
    assert b"Nothing was connected" in page.data
    assert b"dba_checkout.sh" in page.data


def test_run_page_shows_action_plan_and_hides_passwords(app, client):
    response = client.post("/runs", data={"script": "dba_checkout", "server": "db01"})
    job_id = response.location.rstrip("/").split("/")[-1]
    job = _wait(app, job_id)
    assert job.status == "done"
    assert job.outcome.plan["overall_status"] == "WARNING"
    assert job.outcome.exit_code == 0
    page = client.get(f"/runs/{job_id}").get_data(as_text=True)
    assert "Free space in USERS" in page
    assert "hunter2" not in page
    assert "The agent does not run these" in page

    reports = client.get("/reports").get_data(as_text=True)
    report_id = job.outcome.report_dir.name
    assert report_id in reports
    detail = client.get(f"/reports/{report_id}").get_data(as_text=True)
    assert "USERS 97%" in detail
    raw = client.get(f"/reports/{report_id}/raw/db01__dba_checkout.log")
    assert raw.status_code == 200
    assert b"hunter2" not in raw.data
    assert b"[REDACTED]" in raw.data
    assert client.get(f"/reports/{report_id}/raw/..").status_code == 404
    assert client.get("/reports/not-a-report").status_code == 404


def test_api_reports_running_then_done(app, client):
    response = client.post("/runs", data={"script": "os_checkout", "dry_run": "1"})
    job_id = response.location.rstrip("/").split("/")[-1]
    _wait(app, job_id)
    payload = client.get(f"/api/runs/{job_id}").get_json()
    assert payload["status"] == "done"
    assert payload["dry_run"] is True


def test_cli_dry_run_still_prints_commands(config_path, capsys):
    assert main(["-c", str(config_path), "run", "-s", "dba_checkout", "--servers", "db02", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dbadmin" not in out
    assert "oracle@db02.internal" in out
    assert "ccps" in out
