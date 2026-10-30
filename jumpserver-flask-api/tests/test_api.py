import json
import time

from conftest import PASSWORDS, login


def test_health_needs_no_token(client):
    assert client.get("/health").get_json()["status"] == "ok"


def test_login_with_own_username_and_password(client, fake):
    resp = login(client, "jsmith")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["user"] == "jsmith"
    assert len(body["token"]) == 43  # secrets.token_urlsafe(32)
    assert fake.sessions[0].username == "jsmith"
    who = client.get("/session", headers={"X-Session-Token": body["token"]}).get_json()
    assert who["user"] == "jsmith"


def test_wrong_password(client):
    resp = login(client, "jsmith", "wrong")
    assert resp.status_code == 401
    assert "Invalid username or password" in resp.get_json()["error"]


def test_bad_login_bodies(client):
    assert client.post("/session", data="x").status_code == 400
    assert client.post("/session", json={"username": "jsmith"}).status_code == 400
    assert client.post("/session", json={"username": "root;id", "password": "x"}).status_code == 400
    assert client.post("/session", json={"username": "-oProxy", "password": "x"}).status_code == 400


def test_failed_logins_are_rate_limited(client):
    for _ in range(5):
        assert login(client, "jsmith", "wrong").status_code == 401
    resp = login(client, "jsmith")  # even the right password is refused for a while
    assert resp.status_code == 429
    assert "Too many failed logins" in resp.get_json()["error"]


def test_plain_http_login_refused_from_remote_host(client):
    resp = client.post("/session", json={"username": "jsmith", "password": PASSWORDS["jsmith"]},
                       environ_base={"REMOTE_ADDR": "10.1.2.3"})
    assert resp.status_code == 403
    assert "HTTPS" in resp.get_json()["error"]
    resp = client.post("/session", json={"username": "jsmith", "password": PASSWORDS["jsmith"]},
                       base_url="https://localhost", environ_base={"REMOTE_ADDR": "10.1.2.3"})
    assert resp.status_code == 200


def test_allowed_users(config_path, fake):
    from dbaops_api.app import create_app
    data = json.loads(open(config_path).read())
    data["jumpserver"]["allowed_users"] = ["akumar"]
    open(config_path, "w").write(json.dumps(data))
    c = create_app(config_path, connector=fake.connect).test_client()
    assert login(c, "jsmith").status_code == 400
    assert login(c, "akumar").status_code == 200


def test_run_uses_the_logged_in_persons_session(client, headers, fake):
    resp = client.post("/run", headers=headers,
                       json={"host": "dbprod01", "script": "dba_checkout",
                             "confirm": False, "analyze": False})
    assert resp.status_code == 200
    body = resp.get_json()
    assert list(body)[:5] == ["host", "script", "command", "exit_code", "output"]
    assert body["user"] == "jsmith"
    assert body["exit_code"] == 0
    assert body["output"] == "ORACLE_SID=PROD1 OK\n"  # login banner stripped
    assert body["command"].endswith("dbprod01 /dba/bin/dba_checkout")

    user, command, timeout = fake.calls[0]
    assert user == "jsmith"
    assert command.startswith("sudo -S -p __DBAOPS_SUDO_PASSWORD__: su - oracle -c ")
    assert "eval `ssh-agent -s`; ssh-add ~/.ssh/cp993" in command
    assert "timeout 600 ssh" in command
    assert timeout == 630


def test_two_people_get_separate_sessions(client, fake):
    t1 = login(client, "jsmith").get_json()["token"]
    t2 = login(client, "akumar").get_json()["token"]
    client.post("/run", headers={"X-Session-Token": t2}, json={"host": "db1", "script": "showsid"})
    client.post("/run", headers={"X-Session-Token": t1}, json={"host": "db1", "script": "showsid"})
    assert [c[0] for c in fake.calls] == ["akumar", "jsmith"]


def test_logout_closes_ssh(client, headers, fake):
    assert client.delete("/session", headers=headers).status_code == 200
    assert fake.sessions[0].closed
    assert client.get("/scripts", headers=headers).status_code == 401


def test_idle_session_expires(client, headers, app, fake):
    store = app.extensions["dbaops"]["sessions"]
    for s in store._sessions.values():
        s.last_used = time.time() - store.idle - 1
    resp = client.get("/scripts", headers=headers)
    assert resp.status_code == 401
    assert "expired" in resp.get_json()["error"]
    assert fake.sessions[0].closed


def test_password_never_returned(client, headers, fake):
    fake.stdout = f"oops the profile echoed {PASSWORDS['jsmith']}\n"
    out = client.post("/run", headers=headers, json={"host": "db1", "script": "showsid"}).get_json()
    assert PASSWORDS["jsmith"] not in json.dumps(out)
    assert "********" in out["output"]


def test_ccps_passphrase_from_env_is_sent_and_masked(client, headers, fake, monkeypatch):
    monkeypatch.setenv("CCPS_PASSPHRASE", "Test#Phrase9")
    fake.stdout = "debug: Test#Phrase9\n"
    out = client.post("/run", headers=headers, json={"host": "db1", "script": "showsid"}).get_json()
    assert fake.passphrases == ["Test#Phrase9"]
    assert "Test#Phrase9" not in fake.calls[0][1]  # not on the command line
    assert "__DBAOPS_CCPS_PASSPHRASE__:" in fake.calls[0][1]
    assert "Test#Phrase9" not in json.dumps(out)


def test_no_askpass_when_passphrase_not_set(client, headers, fake, monkeypatch):
    monkeypatch.delenv("CCPS_PASSPHRASE", raising=False)
    client.post("/run", headers=headers, json={"host": "db1", "script": "showsid"})
    assert "SSH_ASKPASS" not in fake.calls[0][1]


def test_script_args_are_quoted_as_one_remote_command(client, headers):
    resp = client.post("/run", headers=headers,
                       json={"host": "dbprod01", "script": "maint_mode", "confirm": True})
    assert resp.status_code == 200
    assert resp.get_json()["command"].endswith("dbprod01 '/dba/bin/maint_mode.sh -s'")


def test_missing_and_bad_token(client):
    resp = client.post("/run", json={"host": "dbprod01", "script": "dba_checkout"})
    assert resp.status_code == 401
    assert "Missing session token" in resp.get_json()["error"]
    assert client.get("/scripts", headers={"X-Session-Token": "nope"}).status_code == 401


def test_bearer_header_also_accepted(client, headers):
    token = headers["X-Session-Token"]
    assert client.get("/scripts", headers={"Authorization": f"Bearer {token}"}).status_code == 200


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
    assert client.post("/run", headers=headers, data="host=db01").status_code == 400


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
    assert fake.calls[0][2] == 930


def test_script_nonzero_exit_is_200(client, headers, fake):
    fake.returncode = 3
    fake.stdout = "WARNING: tablespace USERS 97% full\n"
    resp = client.post("/run", headers=headers, json={"host": "db01", "script": "dba_checkout"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["exit_code"] == 3
    assert body["status"] == "script_failed"


def test_ssh_failure_is_502(client, headers, fake):
    fake.returncode = 255
    fake.stdout = ""
    fake.stderr = "ssh: Could not resolve hostname db01\n"
    resp = client.post("/run", headers=headers, json={"host": "db01", "script": "dba_checkout"})
    assert resp.status_code == 502
    assert resp.get_json()["status"] == "ssh_failed"


def test_sudo_not_allowed_is_502(client, headers, fake):
    fake.reach_ssh = False
    fake.returncode = 1
    fake.stderr = "jsmith is not in the sudoers file.  This incident will be reported.\n"
    resp = client.post("/run", headers=headers, json={"host": "db01", "script": "dba_checkout"})
    assert resp.status_code == 502
    body = resp.get_json()
    assert body["status"] == "become_failed"
    assert "jsmith could not switch to oracle" in body["error"]
    assert "not in the sudoers file" in body["error"]


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
    assert resp.get_json()["analysis"] == "plan for db01/dba_checkout"


def test_dry_run_does_not_execute(client, headers, fake):
    resp = client.post("/run", headers=headers,
                       json={"host": "db01", "script": "dba_checkout", "dry_run": True})
    assert resp.status_code == 200
    assert resp.get_json()["jump_command"][:4] == ["sudo", "-S", "-p", "__DBAOPS_SUDO_PASSWORD__:"]
    assert fake.calls == []


def test_scripts_and_tools(client, headers):
    scripts = {s["name"]: s for s in client.get("/scripts", headers=headers).get_json()["scripts"]}
    assert scripts["start_oracle"]["requires_confirm"] is True
    tool = client.get("/tools", headers=headers).get_json()["tools"][0]
    assert "dba_checkout" in tool["function"]["parameters"]["properties"]["script"]["enum"]


def test_audit_log_names_the_person(client, headers, config_path):
    login(client, "jsmith", "wrong")
    client.post("/run", headers=headers, json={"host": "db01", "script": "dba_checkout"})
    audit_path = json.loads(open(config_path).read())["audit_log"]
    records = [json.loads(line) for line in open(audit_path).read().splitlines()]
    assert [(r["event"], r.get("result")) for r in records] == [
        ("login", "ok"), ("login", "failed"), ("run", None)]
    assert records[-1]["user"] == "jsmith"
    assert records[-1]["host"] == "db01"
    assert "Secret" not in open(audit_path).read()
