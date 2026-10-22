from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ConfigError(ValueError):
    pass


@dataclass
class JumpServerConfig:
    host: str
    username: str
    port: int = 22
    key_file: str | None = None
    password_env: str | None = None
    key_passphrase_env: str | None = None
    known_hosts: str | None = "~/.ssh/known_hosts"
    auto_add_host_key: bool = False
    connect_timeout: int = 15
    login_shell: bool = True

    @property
    def password(self) -> str | None:
        return os.environ.get(self.password_env) if self.password_env else None

    @property
    def key_passphrase(self) -> str | None:
        return os.environ.get(self.key_passphrase_env) if self.key_passphrase_env else None


@dataclass
class CcpsConfig:
    command: str = "ccps"
    # "once": run before any script and require exit code 0.
    # "per_command": prefix every target command, for when ccps only affects the current shell.
    mode: str = "once"
    timeout: int = 120


@dataclass
class TargetSshConfig:
    user: str | None = None
    options: list[str] = field(
        default_factory=lambda: ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    )


@dataclass
class ServerConfig:
    name: str
    host: str
    user: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass
class ScriptConfig:
    name: str
    description: str = ""
    remote_path: str | None = None
    local_file: str | None = None
    args: list[str] = field(default_factory=list)
    timeout: int = 600


@dataclass
class LlmConfig:
    provider: str = "openai"
    model: str = "gpt-4o"
    base_url: str | None = None
    api_key_env: str = "LLM_API_KEY"
    api_version: str | None = None
    temperature: float = 0.1
    json_mode: bool = True
    max_chars_per_output: int = 20000
    request_timeout: int = 180

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env)


@dataclass
class AppConfig:
    jumpserver: JumpServerConfig
    ccps: CcpsConfig
    target_ssh: TargetSshConfig
    servers: dict[str, ServerConfig]
    scripts: dict[str, ScriptConfig]
    llm: LlmConfig
    max_parallel: int = 5
    reports_dir: str = "reports"
    redact_patterns: list[str] = field(default_factory=list)
    base_dir: Path = Path(".")

    def select_servers(self, names: list[str] | None, tags: list[str] | None) -> list[ServerConfig]:
        if not names and not tags:
            return list(self.servers.values())
        selected: dict[str, ServerConfig] = {}
        for name in names or []:
            if name not in self.servers:
                raise ConfigError(f"Unknown server '{name}'. Known: {', '.join(self.servers)}")
            selected[name] = self.servers[name]
        for tag in tags or []:
            for server in self.servers.values():
                if tag in server.tags:
                    selected[server.name] = server
        if not selected:
            raise ConfigError("No servers matched the given --servers/--tags")
        return list(selected.values())

    def get_script(self, name: str) -> ScriptConfig:
        if name not in self.scripts:
            raise ConfigError(
                f"Script '{name}' is not in the allowlist. Allowed: {', '.join(self.scripts)}"
            )
        return self.scripts[name]


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            var, default = match.group(1), match.group(2)
            if var in os.environ:
                return os.environ[var]
            if default is not None:
                return default
            raise ConfigError(f"Environment variable '{var}' is referenced in config but not set")

        return _ENV_REF.sub(repl, value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def _build(cls: type, data: dict[str, Any] | None, section: str, **extra: Any) -> Any:
    data = dict(data or {})
    allowed = set(cls.__dataclass_fields__)
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"Unknown key(s) in '{section}': {', '.join(sorted(unknown))}")
    try:
        return cls(**data, **extra)
    except TypeError as exc:
        raise ConfigError(f"Invalid '{section}' section: {exc}") from exc


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    raw = _expand_env(raw)

    if "jumpserver" not in raw:
        raise ConfigError("Missing 'jumpserver' section")

    servers: dict[str, ServerConfig] = {}
    for item in raw.get("servers") or []:
        server = _build(ServerConfig, item, "servers")
        if not _NAME.match(server.name):
            raise ConfigError(f"Invalid server name '{server.name}'")
        if server.name in servers:
            raise ConfigError(f"Duplicate server name '{server.name}'")
        servers[server.name] = server
    if not servers:
        raise ConfigError("At least one entry is required in 'servers'")

    scripts: dict[str, ScriptConfig] = {}
    for name, item in (raw.get("scripts") or {}).items():
        if not _NAME.match(name):
            raise ConfigError(f"Invalid script name '{name}'")
        script = _build(ScriptConfig, item, f"scripts.{name}", name=name)
        if bool(script.remote_path) == bool(script.local_file):
            raise ConfigError(
                f"Script '{name}' must set exactly one of 'remote_path' or 'local_file'"
            )
        if script.local_file:
            local = (path.parent / script.local_file).resolve()
            if not local.is_file():
                raise ConfigError(f"Script '{name}' local_file not found: {local}")
            script.local_file = str(local)
        scripts[name] = script
    if not scripts:
        raise ConfigError("At least one entry is required in 'scripts'")

    ccps = _build(CcpsConfig, raw.get("ccps"), "ccps")
    if ccps.mode not in {"once", "per_command"}:
        raise ConfigError("ccps.mode must be 'once' or 'per_command'")

    llm = _build(LlmConfig, raw.get("llm"), "llm")
    if llm.provider not in {"openai", "azure"}:
        raise ConfigError("llm.provider must be 'openai' (any OpenAI-compatible API) or 'azure'")

    top_level = {"jumpserver", "ccps", "target_ssh", "servers", "scripts", "llm",
                 "max_parallel", "reports_dir", "redact_patterns"}
    unknown = set(raw) - top_level
    if unknown:
        raise ConfigError(f"Unknown top-level key(s): {', '.join(sorted(unknown))}")

    reports_dir = Path(raw.get("reports_dir", "reports"))
    if not reports_dir.is_absolute():
        reports_dir = path.parent / reports_dir

    return AppConfig(
        jumpserver=_build(JumpServerConfig, raw["jumpserver"], "jumpserver"),
        ccps=ccps,
        target_ssh=_build(TargetSshConfig, raw.get("target_ssh"), "target_ssh"),
        servers=servers,
        scripts=scripts,
        llm=llm,
        max_parallel=int(raw.get("max_parallel", 5)),
        reports_dir=str(reports_dir),
        redact_patterns=list(raw.get("redact_patterns") or []),
        base_dir=path.parent,
    )
