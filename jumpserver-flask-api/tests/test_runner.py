import json
import socket

import pytest

from dbaops_api.config import ConfigError, load_config
from dbaops_api.runner import Completed, MARKER, Runner


class Executor:
    def __init__(self, returncode=0, stdout="ok\n", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.argv = None
        self.secret = None

    def exec(self, argv, timeout, ccps_secret):
        self.argv = argv
        self.secret = ccps_secret
        return Completed(
            self.returncode,
            (MARKER + "\n" + self.stdout).encode("utf-8"),
            (MARKER + "\n" + self.stderr).encode("utf-8"),
        )


def configured(config_path, monkeypatch):
    monkeypatch.setenv(
        "DBAOPS_API_TOKEN", "token-with-at-least-thirty-two-characters")
    monkeypatch.setenv("CCPS_SECRET", "Super#Ccps")
    return load_config(config_path)


def resolve_allowed(_host, port, _family, _kind):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.20.4.8", port))]


def test_config_rejects_redirection_of_security_boundaries(
        config_path, monkeypatch, tmp_path):
    configured(config_path, monkeypatch)
    original = json.loads(open(config_path).read())
    for key, value in (
            ("actual_jump_host", "attacker.example"),
            ("script_dir", "/tmp"),
            ("ccps_command", "ccps; id")):
        changed = dict(original)
        changed[key] = value
        path = tmp_path / (key + ".json")
        path.write_text(json.dumps(changed))
        with pytest.raises(ConfigError):
            load_config(str(path))


def test_target_policy_accepts_exact_host(config_path, monkeypatch):
    config = configured(config_path, monkeypatch)
    target = config.validate_target(
        "dbprod01.example.internal", 22, resolver=resolve_allowed)
    assert target.connect_host == "10.20.4.8"
    assert target.host_key_alias == "dbprod01.example.internal"


def test_target_policy_resolves_cidr_host_to_validated_address(
        config_path, monkeypatch):
    config = configured(config_path, monkeypatch)

    def resolver(_host, port, _family, _kind):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.20.4.8", port))]

    target = config.validate_target("database.internal", 22, resolver=resolver)
    assert target.requested_host == "database.internal"
    assert target.connect_host == "10.20.4.8"
    assert target.host_key_alias is None


def test_target_policy_rejects_any_resolution_outside_cidr(
        config_path, monkeypatch):
    config = configured(config_path, monkeypatch)

    def resolver(_host, port, _family, _kind):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.20.4.8", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.9", port)),
        ]

    with pytest.raises(ConfigError):
        config.validate_target("database.internal", 22, resolver=resolver)


@pytest.mark.parametrize("blocked", ["10.17.34.78", "10.17.66.19"])
def test_target_policy_always_rejects_jump_server_addresses(
        config_path, monkeypatch, blocked):
    config = configured(config_path, monkeypatch)
    config.target_allowed_hosts.append(blocked)
    with pytest.raises(ConfigError):
        config.validate_target(blocked, 22)

    def resolver(_host, port, _family, _kind):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (blocked, port))]

    with pytest.raises(ConfigError):
        config.validate_target(
            "dbprod01.example.internal", 22, resolver=resolver)


def test_oracle_shell_contains_no_secret_or_vip(config_path, monkeypatch):
    config = configured(config_path, monkeypatch)
    target = config.validate_target(
        "dbprod01.example.internal", 22, resolver=resolve_allowed)
    runner = Runner(config)
    command = runner.command(target, config.get_script("dba_checkout"))
    assert "Super#Ccps" not in command
    assert "10.17.34.78" not in command
    assert "sudo -n su - oracle -c" in command
    assert "ssh -p 22" in command
    assert "HostKeyAlias=dbprod01.example.internal" in command
    assert "10.20.4.8" in command
    assert "/dba/bin/dba_checkout" in command
    assert "ccps" in command


def test_nonzero_remote_script_result_is_structured(config_path, monkeypatch):
    config = configured(config_path, monkeypatch)
    target = config.validate_target(
        "dbprod01.example.internal", 22, resolver=resolve_allowed)
    executor = Executor(returncode=3, stdout="warning\n")
    result = Runner(config).run(
        executor, target, config.get_script("dba_checkout"))
    assert result.status == "script_failed"
    assert result.exit_code == 3
    assert result.output == "warning\n"
    assert executor.secret == "Super#Ccps"


def test_ssh_failure_is_distinct(config_path, monkeypatch):
    config = configured(config_path, monkeypatch)
    target = config.validate_target(
        "dbprod01.example.internal", 22, resolver=resolve_allowed)
    executor = Executor(returncode=255, stderr="Host key verification failed\n")
    result = Runner(config).run(
        executor, target, config.get_script("dba_checkout"))
    assert result.status == "ssh_failed"
    assert "Host key verification failed" in result.error
