from __future__ import annotations

import threading
import time

import pytest

from conftest import USER_PASSWORD, FakeConnector, needs_pty
from oracle_jump_tool.app import create_app
from oracle_jump_tool.session import SessionManager

pytestmark = needs_pty

KEY = "test-api-key-123"
AUTH = {"Authorization": f"Bearer {KEY}"}
LOGIN = {"username": "jumpuser", "password": USER_PASSWORD}


@pytest.fixture
def env(settings, state):
    settings.api_key = KEY
    connector = FakeConnector(state, FAKE_SUDO_PASSWORD=USER_PASSWORD)
    manager = SessionManager(settings, connector)
    app = create_app(settings, manager)
    yield app.test_client(), manager, connector
    manager.shutdown()


def login(client):
    r = client.post("/login", json=LOGIN)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def test_health_and_scripts_are_public(env):
    client, _, _ = env
    h = client.get("/health").get_json()
    assert h["status"] == "ok" and h["logged_in"] is False and h["jump_host"] == "jump.example.com"
    names = [s["name"] for s in client.get("/scripts").get_json()["scripts"]]
    assert names == ["showsid", "showusers", "dba_checkout", "start_oracle", "maint_mode"]


def test_protected_endpoints_need_key(env):
    client, _, _ = env
    assert client.post("/run", json={"host": "db1", "script": "showsid"}).status_code == 401
    assert client.post("/run", json={"host": "db1", "script": "showsid"},
                       headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/logout").status_code == 401
    assert client.get("/session").status_code == 401


def test_run_before_login_is_409(env):
    client, _, _ = env
    r = client.post("/run", json={"host": "db1", "script": "showsid"}, headers=AUTH)
    assert r.status_code == 409 and r.get_json()["error"] == "not_logged_in"


def test_full_flow(env, state):
    client, manager, _ = env
    body = login(client)
    assert body["token_type"] == "Bearer"
    assert body["session"]["username"] == "jumpuser"
    assert "sudo password sent" in body["session"]["steps"]
    assert client.get("/health").get_json()["logged_in"] is True

    # Session token works as well as the API key.
    tok = {"Authorization": f"Bearer {body['token']}"}
    assert client.get("/session", headers=tok).status_code == 200
    assert client.get("/session", headers={"X-API-Key": KEY}).status_code == 200

    r = client.post("/run", json={"host": "db01.example.com", "script": "showsid"}, headers=AUTH)
    data = r.get_json()
    assert r.status_code == 200 and data["ok"] is True and data["session_closed"] is False
    (res,) = data["results"]
    assert res["host"] == "db01.example.com" and res["script"] == "showsid"
    assert res["exit_code"] == 0 and "ORCL1" in res["stdout"] and res["stderr"] == ""
    assert isinstance(res["duration_s"], float)

    r = client.post("/run", json={"hosts": ["db01", "db02", "unreachable9"],
                                  "script": "/dba/bin/dba_checkout"}, headers=tok)
    data = r.get_json()
    assert data["ok"] is False and data["script"] == "dba_checkout"
    assert [x["status"] for x in data["results"]] == ["failed", "failed", "connection_error"]
    assert data["results"][1]["stderr"].startswith("WARN")

    assert client.post("/logout", headers=AUTH).get_json() == {"logged_out": True}
    assert client.get("/health").get_json()["logged_in"] is False
    # The old session token is no longer valid, but the API key is.
    assert client.get("/session", headers=tok).status_code == 401
    assert client.get("/session", headers=AUTH).status_code == 409


def test_run_validation(env):
    client, _, _ = env
    login(client)
    bad = [
        {"host": "db1; rm -rf /", "script": "showsid"},
        {"host": "db1", "script": "rm -rf /"},
        {"host": "db1", "script": "/dba/bin/showsid && id"},
        {"script": "showsid"},
        {"host": "db1", "script": "showsid", "timeout_s": 999999},
        {"host": "jump.example.com", "script": "showsid"},
        {"hosts": [f"db{i}" for i in range(50)], "script": "showsid"},
    ]
    for body in bad:
        r = client.post("/run", json=body, headers=AUTH)
        assert r.status_code == 400, body
        assert r.get_json()["error"] == "invalid_request"
    assert client.post("/run", data="not json", headers=AUTH).status_code == 400


def test_login_failures(env):
    client, _, _ = env
    first = login(client)
    r = client.post("/login", json={"username": "jumpuser", "password": "wrong"})
    assert r.status_code == 401 and r.get_json()["error"] == "login_failed"
    # A failed attempt leaves the working session alone.
    assert client.get("/session", headers={"Authorization": f"Bearer {first['token']}"}).status_code == 200
    second = login(client)
    assert client.get("/session", headers={"Authorization": f"Bearer {first['token']}"}).status_code == 401
    assert client.get("/session", headers={"Authorization": f"Bearer {second['token']}"}).status_code == 200
    assert client.post("/login", json={"username": "bad user;", "password": "x"}).status_code == 400
    assert client.post("/login", json={"username": "jumpuser"}).status_code == 400


def test_sudo_rejection_surfaces_as_401(settings, state):
    settings.api_key = KEY
    manager = SessionManager(settings, FakeConnector(state, FAKE_SUDO_PASSWORD="different"))
    client = create_app(settings, manager).test_client()
    r = client.post("/login", json=LOGIN)
    body = r.get_json()
    assert r.status_code == 401 and "sudo rejected" in body["message"]
    assert USER_PASSWORD not in body["transcript"]


def test_html_login_page_and_form(env):
    client, _, _ = env
    page = client.get("/")
    assert page.status_code == 200 and b'name="password"' in page.data
    r = client.post("/login", data=LOGIN)
    assert r.status_code == 200 and b"Connected" in r.data and KEY.encode() in r.data


def test_host_header_and_origin_guard(env):
    client, _, _ = env
    assert client.get("/health", headers={"Host": "evil.example.com"}).status_code == 403
    assert client.get("/health", headers={"Host": "localhost:5055"}).status_code == 200
    r = client.post("/login", json=LOGIN, headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403


def test_idle_timeout_expires_session(env, settings):
    client, manager, connector = env
    login(client)
    manager.current().last_used = time.time() - settings.session_idle_timeout - 1
    assert client.get("/health").get_json()["logged_in"] is False
    assert connector.connections[0].channel.closed


def test_concurrent_runs_are_serialized(env, settings):
    client, manager, connector = env
    connector.env["FAKE_SLEEP"] = "2"
    login(client)
    settings.busy_wait_seconds = 0
    out = {}

    def slow():
        out["slow"] = client.post("/run", json={"host": "db1", "script": "start_oracle"}, headers=AUTH)

    t = threading.Thread(target=slow)
    t.start()
    time.sleep(0.5)
    r = client.post("/run", json={"host": "db1", "script": "showsid"}, headers=AUTH)
    assert r.status_code == 503 and r.headers["Retry-After"]
    t.join()
    assert out["slow"].get_json()["results"][0]["stdout"] == "started\n"


def test_openapi_and_tool_schema(env):
    client, _, _ = env
    spec = client.get("/openapi.json").get_json()
    assert spec["openapi"].startswith("3.") and "/run" in spec["paths"]
    tools = client.get("/tool-schema.json").get_json()
    run = next(t for t in tools if t["function"]["name"] == "run_oracle_script")
    assert run["function"]["parameters"]["properties"]["script"]["enum"] == [
        "showsid", "showusers", "dba_checkout", "start_oracle", "maint_mode"]
