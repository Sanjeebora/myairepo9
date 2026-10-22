from __future__ import annotations

import pytest

from conftest import CCPS_PASSWORD, USER_PASSWORD, PtyChannel, fake_env, needs_pty
from oracle_jump_tool.scripts import ALLOWED_SCRIPTS
from oracle_jump_tool.shell import InteractiveShell, JumpShell, ShellError

pytestmark = needs_pty


@pytest.fixture
def make_shell(state, settings):
    channels = []

    def make(noediting=False, **env):
        ch = PtyChannel(fake_env(state, **env), noediting=noediting)
        channels.append(ch)
        sh = InteractiveShell(ch, [USER_PASSWORD, settings.ccps_password])
        return JumpShell(sh, settings)

    yield make
    for ch in channels:
        ch.close()


def test_login_with_sudo_password_then_ccps(make_shell, state):
    js = make_shell(FAKE_SUDO_PASSWORD=USER_PASSWORD)
    js.login(USER_PASSWORD)
    assert js.steps == ["shell ready", "sudo password sent", "became oracle",
                        "ccps password sent", "ccps done (exit 0)"]
    assert (state / "ccps_ok").exists()


def test_slow_sudo_prompt_is_waited_for(make_shell, settings):
    # Slower than the banner quiet window: typing ahead here would feed sudo the wrong line.
    js = make_shell(FAKE_SUDO_PASSWORD=USER_PASSWORD, FAKE_SUDO_DELAY=str(settings.banner_quiet_seconds + 1))
    js.login(USER_PASSWORD)
    assert js.steps[:3] == ["shell ready", "sudo password sent", "became oracle"]


def test_late_prompt_is_not_mistaken_for_become_finishing(make_shell):
    # The jump user's prompt shows up after the sync marker and after the typed command's
    # echo, so it is the last thing in the buffer while sudo starts. It must not end the wait.
    js = make_shell(noediting=True, FAKE_SUDO_PASSWORD=USER_PASSWORD, FAKE_SUDO_DELAY="0.5",
                    PROMPT_COMMAND="sleep 0.3")
    js.login(USER_PASSWORD)
    assert js.steps[:3] == ["shell ready", "sudo password sent", "became oracle"]


def test_login_without_sudo_prompt(make_shell):
    js = make_shell()
    js.login(USER_PASSWORD)
    assert "sudo password sent" not in js.steps
    assert "became oracle" in js.steps


def test_ccps_without_prompt_is_accepted_by_default(make_shell, state):
    js = make_shell(FAKE_CCPS_MODE="noprompt")
    js.login(USER_PASSWORD)
    assert js.steps[-1] == "ccps done (exit 0)"
    assert "ccps password sent" not in js.steps


def test_ccps_require_prompt(make_shell, settings):
    settings.ccps_require_prompt = True
    js = make_shell(FAKE_CCPS_MODE="noprompt")
    with pytest.raises(ShellError, match="never asked"):
        js.login(USER_PASSWORD)


def test_wrong_sudo_password(make_shell):
    js = make_shell(FAKE_SUDO_PASSWORD="something-else")
    with pytest.raises(ShellError, match="sudo rejected"):
        js.login(USER_PASSWORD)


def test_sudo_denied_is_reported(make_shell):
    js = make_shell(FAKE_SUDO_DENY="1")
    with pytest.raises(ShellError, match="Expected to be oracle") as exc:
        js.login(USER_PASSWORD)
    assert "sudoers" in exc.value.transcript


def test_wrong_ccps_password(make_shell, settings):
    settings.ccps_password = "not-the-one"
    js = make_shell()
    with pytest.raises(ShellError, match="rejected the password"):
        js.login(USER_PASSWORD)


def test_ccps_nonzero_exit(make_shell):
    js = make_shell(FAKE_CCPS_EXIT="4")
    with pytest.raises(ShellError, match="exited with code 4"):
        js.login(USER_PASSWORD)


def test_run_splits_stdout_stderr_and_exit_code(make_shell, state):
    js = make_shell()
    js.login(USER_PASSWORD)

    r = js.run("db01.example.com", ALLOWED_SCRIPTS["showsid"], 30)
    assert r.status == "ok" and r.exit_code == 0
    assert r.stdout == "SID      STATUS   HOST\nORCL1    OPEN     db01.example.com\nORCL2    MOUNTED  db01.example.com\n"
    assert r.stderr == ""
    assert r.duration_s >= 0

    r = js.run("db01.example.com", ALLOWED_SCRIPTS["dba_checkout"], 30)
    assert (r.status, r.exit_code) == ("failed", 3)
    assert r.stdout == "Checkout on db01.example.com\n"
    assert r.stderr == "WARN: tablespace USERS 91% full\n"

    r = js.run("db02", ALLOWED_SCRIPTS["maint_mode"], 30)
    assert r.stdout == "maintenance mode: OFF\n"
    assert r.command.endswith("-- db02 '/dba/bin/maint_mode.sh -s'")

    r = js.run("unreachable01", ALLOWED_SCRIPTS["showsid"], 30)
    assert (r.status, r.exit_code) == ("connection_error", 255)
    assert "Could not resolve hostname" in r.stderr

    log = (state / "ssh.log").read_text().splitlines()
    assert log[0].endswith("-- db01.example.com /dba/bin/showsid")
    assert log[2].endswith("-- db02 /dba/bin/maint_mode.sh -s")
    assert "BatchMode=yes" in log[0]


def test_timeout_interrupts_and_shell_recovers(make_shell):
    js = make_shell(FAKE_SLEEP="30")
    js.login(USER_PASSWORD)
    r = js.run("db01", ALLOWED_SCRIPTS["start_oracle"], 2)
    assert r.status == "timeout" and r.exit_code is None
    r = js.run("db01", ALLOWED_SCRIPTS["showusers"], 10)
    assert r.status == "ok"
    assert r.stdout.splitlines() == ["SYS", "SYSTEM", "APPUSER (3 sessions)"]


def test_secrets_never_appear_in_output(make_shell):
    js = make_shell(FAKE_SUDO_PASSWORD=USER_PASSWORD)
    js.login(USER_PASSWORD)
    js.sh.send_line(f"echo {USER_PASSWORD} {CCPS_PASSWORD}")
    js.sync()
    assert USER_PASSWORD not in js.sh.transcript()
    assert CCPS_PASSWORD not in js.sh.transcript()
