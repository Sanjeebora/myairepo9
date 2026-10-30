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
class AppConfig:
    scripts: Dict[str, ScriptConfig]
    script_dir: str = "/dba/bin"
    become: List[str] = field(
        default_factory=lambda: ["sudo", "-n", "su", "-", "oracle", "-c"])
    ccps: str = "eval `ssh-agent -s`; ssh-add ~/.ssh/cp993"
    ssh_user: Optional[str] = None
    ssh_options: List[str] = field(default_factory=lambda: [
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=30",
        "-o", "StrictHostKeyChecking=yes",
    ])
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
    cfg = _build(AppConfig, raw, "config", scripts=scripts, llm=llm)

    if not cfg.script_dir.startswith("/"):
        raise ConfigError("script_dir must be an absolute path")
    if cfg.max_concurrent < 1:
        raise ConfigError("max_concurrent must be at least 1")
    if not isinstance(cfg.become, list) or not all(isinstance(a, str) for a in cfg.become):
        raise ConfigError('become must be a list, for example ["sudo", "-n", "su", "-", "oracle", "-c"]')
    try:
        re.compile(cfg.host_pattern)
    except re.error as exc:
        raise ConfigError(f"host_pattern is not a valid regex: {exc}") from exc
    return cfg
