"""Runs the generated oracle shell for real (ssh-agent, ssh-add, timeout) with a stub `ssh`."""
import json
import os
import shutil
import subprocess

import pytest

from dbaops_api.config import load_config
from dbaops_api.runner import Runner

pytestmark = pytest.mark.skipif(
    not (shutil.which("ssh-agent") and shutil.which("ssh-keygen") and shutil.which("timeout")),
    reason="needs openssh-clients and coreutils")

FAKE_SSH = """#!/bin/sh
echo "args: $*"
echo "keys: $(ssh-add -l 2>/dev/null | wc -l)"
echo "$SSH_AGENT_PID" > "$PIDFILE"
case "$*" in
  *dead*) echo "ssh: connect to host dead port 22: Connection refused" >&2; exit 255 ;;
  *slow*) sleep 30 ;;
  *warn*) echo "WARN tablespace"; exit 3 ;;
esac
echo "checkout OK"
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(0o755)
    key = tmp_path / "cp993"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("PIDFILE", str(tmp_path / "agent.pid"))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.delenv("SSH_AGENT_PID", raising=False)
    return tmp_path, key


def make_runner(tmp_path, key, **overrides):
    data = {
        "become": [],
        "ccps": f"eval `ssh-agent -s`; ssh-add {key}",
        "default_timeout": 5,
        "scripts": {"dba_checkout": {"file": "dba_checkout"},
                    "maint_mode": {"file": "maint_mode.sh", "args": ["-s"]}},
        **overrides,
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return Runner(load_config(str(path)))


def agent_alive(tmp_path):
    pid = int((tmp_path / "agent.pid").read_text().strip())
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_ccps_then_ssh_and_agent_is_cleaned_up(env):
    tmp_path, key = env
    runner = make_runner(tmp_path, key)
    cfg = runner.config
    result = runner.run("dbprod01", cfg.scripts["maint_mode"])
    assert result.status == "ok", result
    assert result.exit_code == 0
    assert "keys: 1" in result.output  # ssh-add loaded the key into this shell's agent
    assert "dbprod01 /dba/bin/maint_mode.sh -s" in result.output
    assert result.output.rstrip().endswith("checkout OK")
    assert "Agent pid" not in result.output
    assert not agent_alive(tmp_path)


def test_script_exit_code_survives_cleanup_trap(env):
    tmp_path, key = env
    runner = make_runner(tmp_path, key)
    result = runner.run("warnhost", runner.config.scripts["dba_checkout"])
    assert result.exit_code == 3
    assert result.status == "script_failed"
    assert not agent_alive(tmp_path)


def test_ssh_connection_failure(env):
    tmp_path, key = env
    runner = make_runner(tmp_path, key)
    result = runner.run("deadhost", runner.config.scripts["dba_checkout"])
    assert result.status == "ssh_failed"
    assert "Connection refused" in result.error


def test_ccps_failure_stops_before_ssh(env):
    tmp_path, key = env
    runner = make_runner(tmp_path, key, ccps="eval `ssh-agent -s`; ssh-add /nonexistent/cp993")
    result = runner.run("dbprod01", runner.config.scripts["dba_checkout"])
    assert result.status == "ccps_failed"
    assert result.exit_code == 97
    assert not (tmp_path / "agent.pid").exists()  # ssh never ran


def test_script_timeout(env):
    tmp_path, key = env
    runner = make_runner(tmp_path, key, default_timeout=1)
    result = runner.run("slowhost", runner.config.scripts["dba_checkout"])
    assert result.status == "timeout"
    assert result.exit_code == 124
    assert not agent_alive(tmp_path)


def test_login_profile_noise_is_not_in_output(env):
    tmp_path, key = env
    runner = make_runner(tmp_path, key)
    runner.config.ccps = "echo 'Welcome to the jump server'; " + runner.config.ccps
    argv = runner.argv("dbprod01", runner.config.scripts["dba_checkout"])
    argv[-1] = "echo 'ORACLE_HOME banner from .bash_profile'\n" + argv[-1]
    proc = subprocess.run(argv, capture_output=True, text=True)
    from dbaops_api.runner import _split
    _, seen, out = _split(proc.stdout)
    assert seen
    assert "banner" not in out and "Welcome" not in out
