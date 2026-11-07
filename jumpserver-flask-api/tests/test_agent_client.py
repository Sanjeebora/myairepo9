import json

import pytest

from examples.agent_client import DbaOpsClient


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(self.payload).encode()


def test_client_rejects_plain_http_unless_private_transport_acknowledged():
    with pytest.raises(ValueError, match="Plain HTTP"):
        DbaOpsClient("http://10.145.23.180:8080", "jsmith", xtoken="test-token")
    DbaOpsClient("http://10.145.23.180:8080", "jsmith", xtoken="test-token",
                 allow_http=True)


def test_client_sends_xtoken_and_returns_structured_result(monkeypatch):
    requests = []
    responses = iter([
        {"token": "session-token"},
        {"host": "db01", "script": "dba_checkout", "exit_code": 0,
         "status": "ok", "output": "healthy\n"},
    ])

    def fake_urlopen(request, **_kwargs):
        requests.append(request)
        return FakeResponse(next(responses))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = DbaOpsClient("https://jump.example", "jsmith", password="runtime-password",
                          xtoken="runtime-gateway-token")
    result = client.run("db01", "dba_checkout")

    assert result == {"host": "db01", "script": "dba_checkout", "exit_code": 0,
                      "status": "ok", "output": "healthy\n"}
    assert [r.full_url for r in requests] == [
        "https://jump.example/session", "https://jump.example/run"]
    assert all(r.get_header("X-token") == "runtime-gateway-token" for r in requests)
    assert "runtime-gateway-token" not in requests[0].data.decode()
    assert requests[1].get_header("X-session-token") == "session-token"
