from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CCPS_PASSWORD = "bake6Pies"

DEFAULT_SUDO_PROMPT = r"(?im)(\[sudo\] password for [^:\r\n]*:|^password( for [^:\r\n]*)?:)\s*$"
DEFAULT_CCPS_PROMPT = r"(?i)(password|passphrase)[^\r\n]*:\s*$"
DEFAULT_SHELL_PROMPT = r"[$#>%]\s*$"
DEFAULT_AUTH_FAILURE = r"(?i)(sorry, try again|incorrect password|authentication failure|bad passphrase)"

DEFAULT_SSH_OPTIONS = (
    "-n -T -o BatchMode=yes -o ConnectTimeout=20 "
    "-o StrictHostKeyChecking=accept-new -o LogLevel=ERROR"
)


def app_dir() -> Path:
    """Folder holding .env and known_hosts: next to the .exe when frozen, else the project root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def load_dotenv_file() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(app_dir() / ".env", override=False)


def _int(env: dict, name: str, default: int, lo: int = 1, hi: int = 10**7) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return value


def _float(env: dict, name: str, default: float, lo: float = 0.1) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not lo <= value <= 600:
        raise ValueError(f"{name} must be between {lo} and 600")
    return value


def _bool(env: dict, name: str, default: bool) -> bool:
    raw = (env.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    bind_host: str = "127.0.0.1"
    bind_port: int = 5055

    jump_host: str = ""
    jump_port: int = 22
    host_key_policy: str = "tofu"  # tofu | strict | insecure
    known_hosts_file: str = ""

    become_command: str = "sudo su - oracle"
    become_user: str = "oracle"
    ccps_command: str = "ccps"
    ccps_password: str = DEFAULT_CCPS_PASSWORD
    ccps_require_prompt: bool = False
    ccps_max_prompts: int = 3
    ccps_ignore_exit_code: bool = False

    sudo_prompt_regex: str = DEFAULT_SUDO_PROMPT
    ccps_prompt_regex: str = DEFAULT_CCPS_PROMPT
    shell_prompt_regex: str = DEFAULT_SHELL_PROMPT
    auth_failure_regex: str = DEFAULT_AUTH_FAILURE

    target_ssh_options: str = DEFAULT_SSH_OPTIONS
    allowed_host_regex: str = ""

    connect_timeout: int = 20
    login_timeout: int = 90
    banner_quiet_seconds: float = 2.0
    quiet_seconds: float = 10.0
    prompt_reply_delay: float = 0.3
    command_timeout: int = 900
    max_command_timeout: int = 3600
    max_hosts: int = 20
    max_output_bytes: int = 2_000_000
    busy_wait_seconds: int = 1800

    session_idle_timeout: int = 1800
    session_max_age: int = 8 * 3600

    api_key: str = ""
    check_host_header: bool = True

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)
        s = cls()
        s.bind_host = env.get("JT_BIND_HOST", s.bind_host).strip() or s.bind_host
        s.bind_port = _int(env, "JT_BIND_PORT", s.bind_port, 1, 65535)
        s.jump_host = env.get("JT_JUMP_HOST", "").strip()
        s.jump_port = _int(env, "JT_JUMP_PORT", s.jump_port, 1, 65535)
        s.host_key_policy = (env.get("JT_HOST_KEY_POLICY") or s.host_key_policy).strip().lower()
        if s.host_key_policy not in {"tofu", "strict", "insecure"}:
            raise ValueError("JT_HOST_KEY_POLICY must be tofu, strict or insecure")
        s.known_hosts_file = (env.get("JT_KNOWN_HOSTS") or str(app_dir() / "known_hosts")).strip()
        s.become_command = env.get("JT_BECOME_COMMAND", s.become_command).strip()
        s.become_user = env.get("JT_BECOME_USER", s.become_user).strip()
        s.ccps_command = env.get("JT_CCPS_COMMAND", s.ccps_command).strip()
        # An empty JT_CCPS_PASSWORD keeps the default; it is not a way to send an empty line.
        s.ccps_password = env.get("JT_CCPS_PASSWORD") or s.ccps_password
        s.ccps_require_prompt = _bool(env, "JT_CCPS_REQUIRE_PROMPT", s.ccps_require_prompt)
        s.ccps_max_prompts = _int(env, "JT_CCPS_MAX_PROMPTS", s.ccps_max_prompts, 1, 10)
        s.ccps_ignore_exit_code = _bool(env, "JT_CCPS_IGNORE_EXIT_CODE", s.ccps_ignore_exit_code)
        s.sudo_prompt_regex = env.get("JT_SUDO_PROMPT_REGEX") or s.sudo_prompt_regex
        s.ccps_prompt_regex = env.get("JT_CCPS_PROMPT_REGEX") or s.ccps_prompt_regex
        s.shell_prompt_regex = env.get("JT_SHELL_PROMPT_REGEX") or s.shell_prompt_regex
        s.auth_failure_regex = env.get("JT_AUTH_FAILURE_REGEX") or s.auth_failure_regex
        s.target_ssh_options = env.get("JT_TARGET_SSH_OPTIONS", s.target_ssh_options)
        s.allowed_host_regex = env.get("JT_ALLOWED_HOST_REGEX", "").strip()
        s.connect_timeout = _int(env, "JT_CONNECT_TIMEOUT", s.connect_timeout, 1, 600)
        s.login_timeout = _int(env, "JT_LOGIN_TIMEOUT", s.login_timeout, 5, 1800)
        s.banner_quiet_seconds = _float(env, "JT_BANNER_QUIET_SECONDS", s.banner_quiet_seconds)
        s.quiet_seconds = _float(env, "JT_QUIET_SECONDS", s.quiet_seconds)
        s.prompt_reply_delay = _float(env, "JT_PROMPT_REPLY_DELAY", s.prompt_reply_delay, 0.0)
        s.command_timeout = _int(env, "JT_COMMAND_TIMEOUT", s.command_timeout, 5, 86400)
        s.max_command_timeout = _int(env, "JT_MAX_COMMAND_TIMEOUT", s.max_command_timeout, 5, 86400)
        s.max_hosts = _int(env, "JT_MAX_HOSTS", s.max_hosts, 1, 500)
        s.busy_wait_seconds = _int(env, "JT_BUSY_WAIT_SECONDS", s.busy_wait_seconds, 0, 86400)
        s.session_idle_timeout = _int(env, "JT_SESSION_IDLE_TIMEOUT", s.session_idle_timeout, 30, 86400 * 7)
        s.session_max_age = _int(env, "JT_SESSION_MAX_AGE", s.session_max_age, 60, 86400 * 30)
        s.api_key = env.get("JT_API_KEY", "").strip()
        s.check_host_header = _bool(env, "JT_CHECK_HOST_HEADER", s.check_host_header)
        return s
