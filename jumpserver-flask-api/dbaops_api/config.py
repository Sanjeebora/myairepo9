from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
# Starts with a letter or digit, so a host can never be read by ssh as an option ("-oProxyCommand=...").
DEFAULT_HOST_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9-]{1,63})*$"


class ConfigError(ValueError):
    pass


@dataclass
class ScriptConfig:
    name: str
    file: str
    description: str = ""
    args: List[str] = field(default_factory=list)
    state_changing: bool = False
    pass_hostname: bool = False
    timeout: Optional[int] = None


@dataclass
class LlmConfig:
    base_url: str = ""
    model: str = ""
    api_key_env: str = "LLM_API_KEY"
    timeout: int = 120
    max_chars: int = 20000

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.model)

    @property
    def api_key(self) -> Optional[str]:
        return os.environ.get(self.api_key_env)


@dataclass
class JumpConfig:
    """Where people log in with their own username and password."""
    host: str = "127.0.0.1"
    port: int = 22
    # Host keys of the jump server, as seen by the account the service runs as.
    known_hosts: str = "~/.ssh/known_hosts"
    connect_timeout: int = 15
    username_pattern: str = r"^[a-z_][a-z0-9_.-]{0,31}$"
    allowed_users: List[str] = field(default_factory=list)
    # Login over plain HTTP is refused unless the request comes from this host
    # (for example nginx terminating TLS in front of the service).
    allow_http_login: bool = False
    max_login_failures: int = 5
    login_failure_window: int = 600
    session_idle_seconds: int = 1800
    # Close stdin this long after start if sudo has not asked for a password.
    sudo_prompt_wait: int = 20


@dataclass
class AppConfig:
    scripts: Dict[str, ScriptConfig]
    jumpserver: JumpConfig = field(default_factory=JumpConfig)
    # Shared gateway credential presented by the remote harness in X-Token.
    # The value itself is always read from the process environment, never JSON.
    xtoken_env: str = "DBAOPS_XTOKEN"
    script_dir: str = "/dba/bin"
    # Runs as the logged-in person. "sudo" gets "-S -p <prompt>" added so their
    # password can be supplied if their sudo rule asks for it.
    become: List[str] = field(
        default_factory=lambda: ["sudo", "su", "-", "oracle", "-c"])
    ccps: str = "eval `ssh-agent -s`; ssh-add ~/.ssh/cp993"
    # Environment variable holding the cp993 passphrase that ssh-add asks for. Empty = no passphrase.
    ccps_passphrase_env: str = "CCPS_PASSPHRASE"
    ssh_user: Optional[str] = None
    ssh_options: List[str] = field(default_factory=lambda: [
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=30",
        "-o", "StrictHostKeyChecking=yes",
    ])
    # Out-of-band SHA-256 fingerprints used only by the operator-run enroll-host command.
    # Runtime SSH remains StrictHostKeyChecking=yes.
    target_host_fingerprints: Dict[str, List[str]] = field(default_factory=dict)
    host_pattern: str = DEFAULT_HOST_PATTERN
    allowed_hosts: List[str] = field(default_factory=list)
    default_timeout: int = 600
    max_concurrent: int = 4
    max_output_bytes: int = 2_000_000
    session_ttl_seconds: int = 8 * 3600
    audit_log: Optional[str] = None
    llm: LlmConfig = field(default_factory=LlmConfig)

    def __post_init__(self) -> None:
        self._host_re = re.compile(self.host_pattern)
        self._user_re = re.compile(self.jumpserver.username_pattern)

    def validate_username(self, username: Any) -> str:
        if not isinstance(username, str) or not self._user_re.match(username):
            raise ConfigError("'username' is required and must be a valid jump server login name")
        if self.jumpserver.allowed_users and username not in self.jumpserver.allowed_users:
            raise ConfigError(f"User '{username}' is not allowed to use this service")
        return username

    @property
    def ccps_passphrase(self) -> Optional[str]:
        return os.environ.get(self.ccps_passphrase_env) if self.ccps_passphrase_env else None

    @property
    def xtoken(self) -> Optional[str]:
        return os.environ.get(self.xtoken_env) if self.xtoken_env else None

    def script_path(self, script: ScriptConfig) -> str:
        return self.script_dir.rstrip("/") + "/" + script.file

    def timeout_for(self, script: ScriptConfig) -> int:
        return script.timeout or self.default_timeout

    def validate_host(self, host: Any) -> str:
        if not isinstance(host, str) or not host:
            raise ConfigError("'host' is required and must be a string")
        host = host.strip()
        if len(host) > 253 or ".." in host or not self._host_re.match(host):
            raise ConfigError(f"Invalid host '{host}'. It does not match the allowed hostname pattern.")
        if self.allowed_hosts and host not in self.allowed_hosts:
            raise ConfigError(f"Host '{host}' is not in allowed_hosts")
        return host

    def get_script(self, name: Any) -> ScriptConfig:
        if not isinstance(name, str) or name not in self.scripts:
            raise ConfigError(
                f"Unknown script '{name}'. Allowed: {', '.join(sorted(self.scripts))}")
        return self.scripts[name]


def _build(cls: type, data: Any, section: str, **extra: Any) -> Any:
    if not isinstance(data, dict):
        raise ConfigError(f"'{section}' must be an object")
    data = {k: v for k, v in data.items() if not k.startswith("_")}
    unknown = set(data) - set(cls.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"Unknown key(s) in '{section}': {', '.join(sorted(unknown))}")
    try:
        return cls(**data, **extra)
    except TypeError as exc:
        raise ConfigError(f"Invalid '{section}': {exc}") from exc


def load_config(path: str) -> AppConfig:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"Config file not found: {p}")
    try:
        raw = json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{p} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("Config must be a JSON object")
    raw = {k: v for k, v in raw.items() if not k.startswith("_")}

    scripts: Dict[str, ScriptConfig] = {}
    for name, item in (raw.pop("scripts", None) or {}).items():
        if not _NAME.match(name):
            raise ConfigError(f"Invalid script name '{name}'")
        script = _build(ScriptConfig, item, f"scripts.{name}", name=name)
        if not _FILE.match(script.file):
            raise ConfigError(
                f"scripts.{name}.file must be a plain file name inside script_dir, got '{script.file}'")
        if not all(isinstance(a, str) for a in script.args):
            raise ConfigError(f"scripts.{name}.args must be a list of strings")
        scripts[name] = script
    if not scripts:
        raise ConfigError("At least one entry is required in 'scripts'")

    llm = _build(LlmConfig, raw.pop("llm", None) or {}, "llm")
    jump = _build(JumpConfig, raw.pop("jumpserver", None) or {}, "jumpserver")
    for label, pattern in (("host_pattern", raw.get("host_pattern", DEFAULT_HOST_PATTERN)),
                           ("jumpserver.username_pattern", jump.username_pattern)):
        try:
            re.compile(pattern)
        except (re.error, TypeError) as exc:
            raise ConfigError(f"{label} is not a valid regex: {exc}") from exc
    cfg = _build(AppConfig, raw, "config", scripts=scripts, llm=llm, jumpserver=jump)

    if not cfg.script_dir.startswith("/"):
        raise ConfigError("script_dir must be an absolute path")
    if cfg.max_concurrent < 1:
        raise ConfigError("max_concurrent must be at least 1")
    if not isinstance(cfg.become, list) or not all(isinstance(a, str) for a in cfg.become):
        raise ConfigError('become must be a list, for example ["sudo", "su", "-", "oracle", "-c"]')
    if not isinstance(cfg.ssh_options, list) or not all(isinstance(a, str) for a in cfg.ssh_options):
        raise ConfigError("ssh_options must be a list of strings")
    options = _parsed_ssh_options(cfg.ssh_options)
    for required in ("batchmode", "stricthostkeychecking"):
        if options.get(required) != ["yes"]:
            raise ConfigError(
                f"ssh_options must set exactly one {required}=yes and must not override it")
    if not isinstance(cfg.xtoken_env, str) or not cfg.xtoken_env:
        raise ConfigError("xtoken_env must name a non-empty environment variable")
    if not isinstance(cfg.target_host_fingerprints, dict):
        raise ConfigError("target_host_fingerprints must be an object")
    fingerprint_re = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
    for host, fingerprints in cfg.target_host_fingerprints.items():
        cfg.validate_host(host)
        if (not isinstance(fingerprints, list) or not fingerprints
                or not all(isinstance(fp, str) and fingerprint_re.match(fp)
                           for fp in fingerprints)):
            raise ConfigError(
                f"target_host_fingerprints.{host} must be a non-empty list of SHA256 fingerprints")
    return cfg


def _parsed_ssh_options(argv: List[str]) -> Dict[str, List[str]]:
    parsed: Dict[str, List[str]] = {}
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "-o":
            index += 1
            if index >= len(argv):
                raise ConfigError("ssh_options has '-o' without a value")
            option = argv[index]
        elif item.startswith("-o") and len(item) > 2:
            option = item[2:]
        else:
            index += 1
            continue
        if "=" in option:
            key, value = option.split("=", 1)
            parsed.setdefault(key.lower(), []).append(value.lower())
        index += 1
    return parsed
