import json

from itsdangerous import URLSafeTimedSerializer

from conftest import API_KEY, SECRET
from dbaops_api.auth import SALT


def test_health_needs_no_token(client):
    assert client.get("/health").get_json()["status"] == "ok"


def test_run_happy_path(client, headers, fake):
    resp = client.post("/run", headers=headers,
                       json={"host": "dbprod01", "script": "dba_checkout",
                             "confirm": False, "analyze": False})
    assert resp.status_code == 200
    body = resp.get_json()
    assert list(body)[:5] == ["host", "script", "command", "exit_code", "output"]
    assert body["host"] == "dbprod01"
    assert body["exit_code"] == 0
    assert body["output"] == "ORACLE_SID=PROD1 OK\n"  # login banner stripped
    assert body["command"].startswith("ssh -o BatchMode=yes")
    assert body["command"].endswith("dbprod01 /dba/bin/dba_checkout")

    argv, timeout = fake.calls[0]
    assert argv[:6] == ["sudo", "-n", "su", "-", "oracle", "-c"]
    assert "eval `ssh-agent -s`; ssh-add ~/.ssh/cp993" in argv[6]
    assert "timeout 600 ssh" in argv[6]
    assert timeout == 630


def test_script_args_are_quoted_as_one_remote_command(client, headers, fake):
    resp = client.post("/run", headers=headers,
                       json={"host": "dbprod01", "script": "maint_mode", "confirm": True})
    assert resp.status_code == 200
    assert resp.get_json()["command"].endswith("dbprod01 '/dba/bin/maint_mode.sh -s'")


def test_missing_token(client):
    resp = client.post("/run", json={"host": "dbprod01", "script": "dba_checkout"})
    assert resp.status_code == 401
    assert "Missing session token" in resp.get_json()["error"]


def test_bad_token(client):
    resp = client.get("/scripts", headers={"X-Session-Token": "nope"})
    assert resp.status_code == 401


def test_expired_token(client):
    old = URLSafeTimedSerializer(SECRET, salt=SALT)
    import itsdangerous.timed as timed
    original = timed.TimestampSigner.get_timestamp
    timed.TimestampSigner.get_timestamp = lambda self: 1_000_000_000
    try:
        token = old.dumps({"sub": "x"})
    finally:
        timed.TimestampSigner.get_timestamp = original
    resp = client.get("/scripts", headers={"X-Session-Token": token})
    assert resp.status_code == 401
    assert "expired" in resp.get_json()["error"]


def test_bearer_header_also_accepted(client, sessions):
    token, _ = sessions.issue()
    assert client.get("/scripts", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_session_endpoint(client):
    assert client.post("/session").status_code == 401
    assert client.post("/session", headers={"X-API-Key": "wrong"}).status_code == 401
    resp = client.post("/session", headers={"X-API-Key": API_KEY}, json={"client": "harness"})
    assert resp.status_code == 200
    token = resp.get_json()["token"]
    assert client.get("/tools", headers={"X-Session-Token": token}).status_code == 200


def test_bad_hosts_rejected(client, headers, fake):
    for host in ["-oProxyCommand=id", "db01;id", "db01 x", "$(id)", "", "a..b", None, 5]:
        resp = client.post("/run", headers=headers, json={"host": host, "script": "dba_checkout"})
        assert resp.status_code == 400, host
    assert fake.calls == []


def test_unknown_script(client, headers, fake):
    for script in ["rm -rf /", "../../bin/sh", "nope"]:
        resp = client.post("/run", headers=headers, json={"host": "db01", "script": script})
        assert resp.status_code == 400
    assert fake.calls == []


def test_non_json_body(client, headers):
    resp = client.post("/run", headers=headers, data="host=db01")
    assert resp.status_code == 400


def test_state_changing_requires_confirm(client, headers, fake):
    for body in [{}, {"confirm": False}]:
        resp = client.post("/run", headers=headers,
                           json={"host": "dbprod01", "script": "start_oracle", **body})
        assert resp.status_code == 409
    assert client.post("/run", headers=headers,
                       json={"host": "dbprod01", "script": "start_oracle",
                             "confirm": "true"}).status_code == 400
    assert fake.calls == []
    resp = client.post("/run", headers=headers,
                       json={"host": "dbprod01", "script": "start_oracle", "confirm": True})
    assert resp.status_code == 200
    assert fake.calls[0][1] == 930


def test_script_nonzero_exit_is_200(client, headers, fake):
    fake.returncode = 3
    fake.stdout = "WARNING: tablespace USERS 97% full\n"
    resp = client.post("/run", headers=headers, json={"host": "db01", "script": "dba_checkout"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["exit_code"] == 3
    assert body["status"] == "script_failed"
    assert "USERS 97%" in body["output"]


def test_ssh_failure_is_502(client, headers, fake):
    fake.returncode = 255
    fake.stdout = ""
    fake.stderr = "ssh: Could not resolve hostname db01\n"
    resp = client.post("/run", headers=headers, json={"host": "db01", "script": "dba_checkout"})
    assert resp.status_code == 502
    assert resp.get_json()["status"] == "ssh_failed"
    assert "Could not resolve" in resp.get_json()["error"]


def test_sudo_failure_is_502(client, headers, fake):
    fake.reach_ssh = False
    fake.returncode = 1
    fake.stderr = "sudo: a password is required\n"
    resp = client.post("/run", headers=headers, json={"host": "db01", "script": "dba_checkout"})
    assert resp.status_code == 502
    body = resp.get_json()
    assert body["status"] == "become_failed"
    assert "a password is required" in body["error"]


def test_ccps_failure_is_502(client, headers, fake):
    fake.reach_ssh = False
    fake.returncode = 97
    fake.stderr = "Could not open a connection to your authentication agent.\n__DBAOPS_CCPS_FAILED__\n"
    resp = client.post("/run", headers=headers, json={"host": "db01", "script": "dba_checkout"})
    assert resp.status_code == 502
    assert resp.get_json()["status"] == "ccps_failed"


def test_script_timeout_is_502(client, headers, fake):
    fake.returncode = 124
    resp = client.post("/run", headers=headers, json={"host": "db01", "script": "dba_checkout"})
    assert resp.status_code == 502
    assert resp.get_json()["status"] == "timeout"


def test_analyze_calls_llm(client, headers):
    resp = client.post("/run", headers=headers,
                       json={"host": "db01", "script": "dba_checkout", "analyze": True})
    assert resp.status_code == 200
    assert resp.get_json()["analysis"] == "plan for db01/dba_checkout"


def test_dry_run_does_not_execute(client, headers, fake):
    resp = client.post("/run", headers=headers,
                       json={"host": "db01", "script": "dba_checkout", "dry_run": True})
    assert resp.status_code == 200
    assert resp.get_json()["jump_command"][:2] == ["sudo", "-n"]
    assert fake.calls == []


def test_scripts_and_tools(client, headers):
    scripts = {s["name"]: s for s in client.get("/scripts", headers=headers).get_json()["scripts"]}
    assert scripts["start_oracle"]["requires_confirm"] is True
    assert scripts["dba_checkout"]["path"] == "/dba/bin/dba_checkout"
    tool = client.get("/tools", headers=headers).get_json()["tools"][0]
    assert tool["function"]["name"] == "run_dba_script"
    assert "dba_checkout" in tool["function"]["parameters"]["properties"]["script"]["enum"]


def test_audit_log_written(client, headers, config_path):
    client.post("/run", headers=headers, json={"host": "db01", "script": "dba_checkout"})
    audit_path = json.loads(open(config_path).read())["audit_log"]
    record = json.loads(open(audit_path).read().strip().splitlines()[-1])
    assert record["event"] == "run"
    assert record["client"] == "pytest"
    assert record["host"] == "db01"
