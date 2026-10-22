import json
import textwrap
from pathlib import Path

import pytest

from ops_agent.config import ConfigError, load_config
from ops_agent.llm import analyze, build_user_prompt, parse_plan
from ops_agent.redact import Redactor, truncate
from ops_agent.report import write_report
from ops_agent.ssh_runner import CommandTimeout, ExecResult, JumpServerRunner

CONFIG = """
jumpserver:
  host: jump.local
  username: ${TEST_JUMP_USER:-ops}
ccps:
  command: ccps
  mode: {mode}
target_ssh:
  user: oracle
servers:
  - name: db01
    host: db01.internal
    tags: [prod]
  - name: db02
    host: db02.internal
    user: dbadmin
    tags: [uat]
scripts:
  dba_checkout:
    remote_path: /home/oracle/scripts/dba_checkout.sh
    args: ["--full"]
  os_checkout:
    local_file: os.sh
llm:
  model: test-model
reports_dir: out
"""


@pytest.fixture
def make_config(tmp_path: Path):
    def _make(mode: str = "once", body: str = CONFIG):
        (tmp_path / "os.sh").write_text("echo hello\n")
        path = tmp_path / "config.yaml"
        path.write_text(textwrap.dedent(body).replace("{mode}", mode))
        return load_config(path)

    return _make


class FakeExecutor:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def exec(self, command, stdin=None, timeout=600):
        self.calls.append((command, stdin))
        for needle, response in self.responses.items():
            if needle in command:
                if isinstance(response, Exception):
                    raise response
                return response
        return ExecResult(0, "", "")


class FakeModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, system, user):
        self.prompts.append(user)
        return self.replies.pop(0)


def test_config_loads_and_expands_env_defaults(make_config):
    cfg = make_config()
    assert cfg.jumpserver.username == "ops"
    assert set(cfg.servers) == {"db01", "db02"}
    assert cfg.scripts["os_checkout"].local_file.endswith("os.sh")
    assert [s.name for s in cfg.select_servers(None, ["uat"])] == ["db02"]


def test_unknown_script_is_rejected(make_config):
    cfg = make_config()
    with pytest.raises(ConfigError, match="allowlist"):
        cfg.get_script("rm_rf")


def test_script_needs_exactly_one_source(make_config):
    bad = CONFIG.replace("    remote_path: /home/oracle/scripts/dba_checkout.sh\n", "")
    with pytest.raises(ConfigError, match="exactly one"):
        make_config(body=bad)


def test_sudo_su_oracle_runs_before_ccps_and_ssh(make_config):
    import subprocess

    cfg = make_config()
    assert cfg.jumpserver.become == "sudo su - oracle"
    runner = JumpServerRunner(cfg, FakeExecutor({}))
    ccps = runner.wrap(cfg.ccps.command)
    ssh = runner.build_command(cfg.servers["db01"], cfg.scripts["dba_checkout"])
    for command in (ccps, ssh):
        assert "sudo su - oracle -c" in command
        subprocess.run(["bash", "-n", "-c", command], check=True)
    assert ccps.index("sudo su - oracle") < ccps.index("ccps")
    assert "ccps" not in ssh
    assert ssh.index("sudo su - oracle") < ssh.index("dba_checkout.sh")

    per = JumpServerRunner(make_config(mode="per_command"), FakeExecutor({}))
    same_shell = per.build_command(cfg.servers["db01"], cfg.scripts["dba_checkout"])
    assert same_shell.index("{ ccps;") < same_shell.index("ssh")
    assert "sudo su - oracle -c" in same_shell

    disabled = CONFIG.replace("  host: jump.local\n", "  host: jump.local\n  become: ''\n")
    plain = JumpServerRunner(make_config(body=disabled), FakeExecutor({}))
    bare = plain.wrap("ccps")
    assert "sudo" not in bare and "su - oracle" not in bare


def test_pass_hostname_appends_host_after_fixed_args(make_config):
    cfg = make_config()
    script = cfg.scripts["dba_checkout"]
    script.remote_path = "/dba/bin/maint_mode.sh"
    script.args = ["-s"]
    runner = JumpServerRunner(cfg, FakeExecutor({}))

    plain = runner.build_command(cfg.servers["db01"], script)
    assert "/dba/bin/maint_mode.sh -s" in plain
    assert plain.count("db01.internal") == 1

    with_host = runner.build_command(cfg.servers["db01"], script, pass_hostname=True)
    assert "/dba/bin/maint_mode.sh -s db01.internal" in with_host
    assert with_host.count("db01.internal") == 2


def test_hostname_option_targets_that_host_and_can_be_passed_to_the_script(make_config):
    cfg = make_config()
    targets = cfg.resolve_targets(None, None, ["spare.example.com"])
    assert [server.host for server in targets] == ["spare.example.com"]
    assert [server.name for server in cfg.resolve_targets(None, None, ["db02.internal"])] == ["db02"]

    with pytest.raises(ConfigError, match="Invalid hostname"):
        cfg.resolve_targets(None, None, ["db01.internal;id"])

    cfg.scripts["dba_checkout"].args = ["{hostname}"]
    cfg.scripts["dba_checkout"].pass_hostname = True
    runner = JumpServerRunner(cfg, FakeExecutor({}))
    command = runner.build_command(cfg.servers["db01"], cfg.scripts["dba_checkout"])
    assert "oracle@db01.internal" in command
    assert command.count("db01.internal") == 2
    assert "db02" not in command


def test_build_command_quotes_and_uses_users(make_config):
    cfg = make_config()
    runner = JumpServerRunner(cfg, FakeExecutor({}))
    cmd = runner.build_command(cfg.servers["db01"], cfg.scripts["dba_checkout"])
    assert cmd.startswith("bash -lc ")
    assert "oracle@db01.internal" in cmd
    assert "BatchMode=yes" in cmd
    assert "/home/oracle/scripts/dba_checkout.sh --full" in cmd

    cmd2 = runner.build_command(cfg.servers["db02"], cfg.scripts["os_checkout"])
    assert "dbadmin@db02.internal" in cmd2
    assert "bash -s --" in cmd2


def test_per_command_mode_prefixes_ccps(make_config):
    cfg = make_config(mode="per_command")
    runner = JumpServerRunner(cfg, FakeExecutor({}))
    cmd = runner.build_command(cfg.servers["db01"], cfg.scripts["dba_checkout"])
    assert "{ ccps; } 1>&2 && ssh" in cmd


def test_ccps_failure_raises(make_config):
    cfg = make_config()
    runner = JumpServerRunner(cfg, FakeExecutor({"ccps": ExecResult(1, "", "not authorized")}))
    with pytest.raises(RuntimeError, match="not authorized"):
        runner.run_ccps()


def test_run_many_classifies_results_and_streams_local_script(make_config):
    cfg = make_config()
    executor = FakeExecutor({
        "db01.internal": ExecResult(0, "TABLESPACE USERS 97% used", ""),
        "dbadmin@db02.internal": CommandTimeout("Command exceeded 600s"),
    })
    runner = JumpServerRunner(cfg, executor)
    results = runner.run_many(list(cfg.servers.values()), [cfg.scripts["os_checkout"]])
    by_server = {r.server: r for r in results}
    assert by_server["db01"].status == "ok"
    assert by_server["db02"].status == "timeout"
    assert all(stdin == b"echo hello\n" for _, stdin in executor.calls)

    executor.responses = {"db01": ExecResult(255, "", "Permission denied (publickey)")}
    result = runner.run_script(cfg.servers["db01"], cfg.scripts["dba_checkout"])
    assert result.status == "connection_error"


def test_redaction():
    redact = Redactor([r"SECRET-\d+"])
    text = ("password=hunter2\nALTER USER x IDENTIFIED BY Sup3r;\n"
            "sqlplus system/manager@ORCL\nid SECRET-123")
    out = redact(text)
    for leaked in ("hunter2", "Sup3r", "manager", "SECRET-123"):
        assert leaked not in out
    assert "system/[REDACTED]@ORCL" in out


def test_truncate_keeps_head_and_tail():
    text = "A" * 100 + "B" * 100
    out = truncate(text, 50)
    assert out.startswith("A") and out.endswith("B") and "omitted" in out


def test_parse_plan_handles_fenced_json():
    assert parse_plan('Here:\n```json\n{"overall_status": "OK"}\n```')["overall_status"] == "OK"


def test_analyze_retries_on_bad_json_and_redacts(make_config):
    cfg = make_config()
    from ops_agent.ssh_runner import ScriptResult

    results = [ScriptResult("db01", "db01.internal", "dba_checkout", "ok", 0,
                            "password=topsecret\nARCH dest 95% full", "", 1.0, "cmd")]
    plan = {"overall_status": "WARNING", "summary": "Archive dest nearly full",
            "servers": [{"name": "db01", "status": "WARNING", "findings": [
                {"severity": "HIGH", "title": "Archive dest 95%", "evidence": "ARCH dest 95% full",
                 "impact": "DB hangs when full"}]}],
            "action_plan": [{"priority": 1, "title": "Free archive space", "servers": ["db01"],
                             "reason": "95% full", "steps": ["Check backups"],
                             "suggested_commands": ["df -h /arch"], "risk": "low", "owner": "DBA"}],
            "needs_human_review": []}
    model = FakeModel(["not json", json.dumps(plan)])
    out = analyze(results, model, cfg.llm, Redactor())
    assert out["overall_status"] == "WARNING"
    assert len(model.prompts) == 2
    assert "topsecret" not in model.prompts[0]

    report_dir = write_report(cfg.reports_dir, out, results, Redactor())
    md = (report_dir / "report.md").read_text()
    assert "Free archive space" in md and "df -h /arch" in md
    raw = (report_dir / "raw" / "db01__dba_checkout.log").read_text()
    assert "topsecret" not in raw
    assert json.loads((report_dir / "report.json").read_text())["plan"]["overall_status"] == "WARNING"


def test_prompt_includes_failures():
    from ops_agent.ssh_runner import ScriptResult

    r = ScriptResult("db02", "h", "dba_checkout", "connection_error", 255, "", "Permission denied", 0.2, "c")
    prompt = build_user_prompt([r], Redactor(), 1000)
    assert "connection_error" in prompt and "Permission denied" in prompt
