import json

from conftest import run_request


def test_health_is_public(client):
    assert client.get("/health").status_code == 200


def test_x_token_is_required(client):
    response = run_request(client, {})
    assert response.status_code == 401
    assert response.get_json()["error"] == "Missing or invalid X-Token"


def test_run_uses_fixed_jump_host_and_allowlisted_path(client, token_header, fake):
    response = run_request(client, token_header)
    assert response.status_code == 200
    body = response.get_json()
    assert body["jump_host"] == "10.17.34.78"
    assert body["command"] == "/dba/bin/dba_checkout"
    assert body["output"] == "database OK\n"
    username, command, timeout, ccps_secret = fake.calls[0]
    assert username == "jsmith"
    assert "sudo -S -p __DBAOPS_SUDO_PASSWORD__:" in command
    assert "su - oracle -c" in command
    assert "ccps" in command
    assert "/dba/bin/dba_checkout" in command
    assert "Login#Secret" not in command
    assert "Ccps#Secret" not in command
    assert ccps_secret == "Ccps#Secret"
    assert timeout == 630
    assert fake.sessions[0].closed


def test_only_four_scripts_are_exposed(client, token_header):
    response = client.get("/scripts", headers=token_header)
    names = sorted(item["name"] for item in response.get_json()["scripts"])
    assert names == ["dba_checkout", "maint_mode.sh", "start_oracle", "stop_oracle"]


def test_arbitrary_commands_and_fields_are_rejected(client, token_header, fake):
    for script in ("id", "../../bin/sh", "dba_checkout; id"):
        response = run_request(client, token_header, script=script)
        assert response.status_code == 400
    response = run_request(client, token_header, command="id")
    assert response.status_code == 400
    assert fake.calls == []


def test_state_changes_require_boolean_confirmation(client, token_header, fake):
    for script in ("start_oracle", "stop_oracle", "maint_mode.sh"):
        assert run_request(client, token_header, script=script).status_code == 409
        assert run_request(client, token_header, script=script, confirm="true").status_code == 400
        assert run_request(client, token_header, script=script, confirm=True).status_code == 200
    assert len(fake.calls) == 3


def test_plain_http_is_rejected(client, token_header):
    response = client.post("/run", headers=token_header, json={
        "username": "jsmith", "password": "Login#Secret", "script": "dba_checkout"
    }, base_url="http://gateway.example.internal")
    assert response.status_code == 403


def test_loopback_tls_proxy_header_is_accepted(client, token_header):
    headers = dict(token_header)
    headers["X-Forwarded-Proto"] = "https"
    response = client.post("/run", headers=headers, json={
        "username": "jsmith", "password": "Login#Secret", "script": "dba_checkout"
    }, base_url="http://127.0.0.1", environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert response.status_code == 200


def test_credentials_and_secrets_are_redacted(client, token_header, fake, config_path):
    fake.stdout = "password=Login#Secret ccps=Ccps#Secret\n"
    response = run_request(client, token_header)
    text = json.dumps(response.get_json())
    assert "Login#Secret" not in text
    assert "Ccps#Secret" not in text
    audit = json.loads(open(config_path).read())["audit_log"]
    audit_text = open(audit).read()
    assert "Login#Secret" not in audit_text
    assert "Ccps#Secret" not in audit_text
    assert "api-test-token-with-at-least-32-bytes" not in audit_text


def test_bad_login_returns_generic_401(client, token_header):
    response = run_request(client, token_header, password="wrong")
    assert response.status_code == 401
    assert response.get_json()["error"] == "Jump-server credentials were rejected"
