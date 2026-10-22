"""End to end through a real paramiko SSH server whose shell is the fake jump server."""
from __future__ import annotations

import paramiko
import pytest

from conftest import USER_PASSWORD, needs_pty
from fake_ssh_server import FakeSSHServer
from oracle_jump_tool.scripts import ALLOWED_SCRIPTS
from oracle_jump_tool.session import LoginError, SessionManager

pytestmark = needs_pty


@pytest.fixture
def ssh_port(state):
    server = FakeSSHServer(state, FAKE_SUDO_PASSWORD=USER_PASSWORD).start()
    yield server.port
    server.stop()


def test_login_and_run_over_real_ssh(ssh_port, settings, tmp_path):
    settings.jump_host = "127.0.0.1"
    settings.jump_port = ssh_port
    settings.known_hosts_file = str(tmp_path / "known_hosts")
    manager = SessionManager(settings)
    try:
        session = manager.login("jumpuser", USER_PASSWORD)
        assert "sudo password sent" in session.shell.steps
        assert session.shell.steps[-1] == "ccps done (exit 0)"
        assert "127.0.0.1" in (tmp_path / "known_hosts").read_text()  # trust on first use
        results, closed = manager.run(["db01", "db02"], ALLOWED_SCRIPTS["showusers"], 30)
        assert not closed
        assert [r.status for r in results] == ["ok", "ok"]
        assert results[1].stdout.splitlines()[0] == "SYS"
    finally:
        manager.shutdown()


def test_wrong_password_over_real_ssh(ssh_port, settings, tmp_path):
    settings.jump_port = ssh_port
    settings.known_hosts_file = str(tmp_path / "known_hosts")
    manager = SessionManager(settings)
    with pytest.raises(LoginError) as exc:
        manager.login("jumpuser", "wrong", "127.0.0.1")
    assert exc.value.status == 401


def test_changed_host_key_is_rejected(ssh_port, settings, tmp_path):
    kh = tmp_path / "known_hosts"
    other = paramiko.RSAKey.generate(1024)
    kh.write_text(f"[127.0.0.1]:{ssh_port} ssh-rsa {other.get_base64()}\n")
    settings.jump_port = ssh_port
    settings.known_hosts_file = str(kh)
    manager = SessionManager(settings)
    with pytest.raises(LoginError) as exc:
        manager.login("jumpuser", USER_PASSWORD, "127.0.0.1")
    assert exc.value.stage == "host_key"
