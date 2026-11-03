import json

from dbaops_api.config import ConfigError, load_config
from dbaops_api.runner import Completed, MARKER, Runner


class Session:
    username = "jsmith"

    def __init__(self, returncode=0, stdout="ok\n", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.command = None
        self.secret = None

    def exec(self, command, timeout, ccps_secret=None):
        self.command = command
        self.secret = ccps_secret
        return Completed(
            self.returncode,
            (MARKER + "\n" + self.stdout).encode("utf-8"),
            (MARKER + "\n" + self.stderr).encode("utf-8"),
        )

    def redact(self, text):
        return text


def test_config_rejects_redirection_of_security_boundaries(config_path, monkeypatch, tmp_path):
    monkeypatch.setenv("DBAOPS_API_TOKEN", "token-with-at-least-thirty-two-characters")
    monkeypatch.setenv("CCPS_SECRET", "secret")
    original = json.loads(open(config_path).read())
    for key, value in (
            ("jump_host", "attacker.example"),
            ("script_dir", "/tmp"),
            ("ccps_command", "ccps; id")):
        changed = dict(original)
        changed[key] = value
        path = tmp_path / (key + ".json")
        path.write_text(json.dumps(changed))
        try:
            load_config(str(path))
            assert False, "%s should have failed" % key
        except ConfigError:
            pass


def test_oracle_shell_contains_no_secret(config_path, monkeypatch):
    monkeypatch.setenv("DBAOPS_API_TOKEN", "token-with-at-least-thirty-two-characters")
    monkeypatch.setenv("CCPS_SECRET", "Super#Ccps")
    config = load_config(config_path)
    runner = Runner(config)
    command = runner.command(config.get_script("dba_checkout"))
    assert "Super#Ccps" not in command
    assert "/dba/bin/dba_checkout" in command
    assert "ccps" in command


def test_nonzero_script_result_is_structured(config_path, monkeypatch):
    monkeypatch.setenv("DBAOPS_API_TOKEN", "token-with-at-least-thirty-two-characters")
    monkeypatch.setenv("CCPS_SECRET", "Super#Ccps")
    config = load_config(config_path)
    session = Session(returncode=3, stdout="warning\n")
    result = Runner(config).run(session, config.get_script("dba_checkout"))
    assert result.status == "script_failed"
    assert result.exit_code == 3
    assert result.output == "warning\n"
    assert session.secret == "Super#Ccps"
