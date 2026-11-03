import json

from conftest import run_request


def test_health_is_public(client):
    assert client.get("/health").status_code == 200


def test_x_token_is_required(client):
    response = run_request(client, {})
    assert response.status_code == 401
    assert response.get_json()["error"] == "Missing or invalid X-Token"


def test_run_executes_locally_then_sshes_to_target(client, token_header, fake):
    response = run_request(client, token_header)
    assert response.status_code == 200
    body = response.get_json()
    assert body["jump_host"] == "10.17.66.19"
    assert body["target_host"] == "dbprod01.example.internal"
    assert body["target_port"] == 22
    assert body["output"] == "database OK\n"
    argv, timeout, ccps_secret = fake.calls[0]
    command = " ".join(argv)
    assert argv[:6] == ["sudo", "-n", "su", "-", "oracle", "-c"]
    assert "ccps" in command
    assert "ssh -p 22" in command
    assert "dbprod01.example.internal" in command
    assert "/dba/bin/dba_checkout" in command
    assert "10.17.34.78" not in command
    assert "Ccps#Secret" not in command
    assert ccps_secret == "Ccps#Secret"
    assert timeout == 630


def test_only_four_scripts_are_exposed(client, token_header):
    response = client.get("/scripts", headers=token_header)
    names = sorted(item["name"] for item in response.get_json()["scripts"])
    assert names == ["dba_checkout", "maint_mode.sh", "start_oracle", "stop_oracle"]


def test_arbitrary_commands_and_fields_are_rejected(client, token_header, fake):
    for script in ("id", "../../bin/sh", "dba_checkout; id"):
        assert run_request(client, token_header, script=script).status_code == 400
    assert run_request(client, token_header, command="id").status_code == 400
    assert run_request(client, token_header, username="jsmith").status_code == 400
    assert run_request(client, token_header, password="secret").status_code == 400
    assert fake.calls == []


def test_target_and_port_are_strictly_validated(client, token_header, fake):
    invalid_hosts = [
        "-oProxyCommand=id", "db01;id", "db01 x", "$(id)", "", None, 5,
        "203.0.113.5",
    ]
    for host in invalid_hosts:
        assert run_request(client, token_header, target_host=host).status_code == 400
    for port in (0, 23, 65536, "22", True, None):
        assert run_request(client, token_header, target_port=port).status_code == 400
    assert fake.calls == []


def test_state_changes_require_boolean_confirmation(client, token_header, fake):
    for script in ("start_oracle", "stop_oracle", "maint_mode.sh"):
        assert run_request(client, token_header, script=script).status_code == 409
        assert run_request(
            client, token_header, script=script, confirm="true").status_code == 400
        assert run_request(
            client, token_header, script=script, confirm=True).status_code == 200
    assert len(fake.calls) == 3


def test_plain_http_policy_is_explicit(client, token_header, app):
    # The sample enables HTTP solely because the clarified BIG-IP VIP is :80.
    response = client.post("/run", headers=token_header, json={
        "target_host": "dbprod01.example.internal",
        "target_port": 22,
        "script": "dba_checkout",
    }, base_url="http://10.17.34.78")
    assert app.extensions["dbaops"]["config"].allow_insecure_http is True
    assert response.status_code == 200


def test_tls_header_is_trusted_only_from_configured_proxy(
        client, token_header, app):
    app.extensions["dbaops"]["config"].allow_insecure_http = False
    headers = dict(token_header)
    headers["X-Forwarded-Proto"] = "https"
    body = {
        "target_host": "dbprod01.example.internal",
        "target_port": 22,
        "script": "dba_checkout",
    }
    trusted = client.post(
        "/run", headers=headers, json=body, base_url="http://10.17.66.19:8080",
        environ_base={"REMOTE_ADDR": "10.17.34.78"})
    untrusted = client.post(
        "/run", headers=headers, json=body, base_url="http://10.17.66.19:8080",
        environ_base={"REMOTE_ADDR": "10.1.1.1"})
    assert trusted.status_code == 200
    assert untrusted.status_code == 403


def test_ccps_secret_is_redacted(client, token_header, fake, config_path):
    fake.stdout = "ccps=Ccps#Secret\n"
    response = run_request(client, token_header)
    text = json.dumps(response.get_json())
    assert "Ccps#Secret" not in text
    audit = json.loads(open(config_path).read())["audit_log"]
    audit_text = open(audit).read()
    assert "Ccps#Secret" not in audit_text
    assert "api-test-token-with-at-least-32-bytes" not in audit_text
    assert "dbprod01.example.internal" in audit_text
