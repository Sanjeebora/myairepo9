import json
import os
import re
from pathlib import Path

JUMP_HOST = "10.17.34.78"
ALLOWED_SCRIPTS = {
    "dba_checkout": "dba_checkout",
    "start_oracle": "start_oracle",
    "stop_oracle": "stop_oracle",
    "maint_mode.sh": "maint_mode.sh",
}
STATE_CHANGING = frozenset(("start_oracle", "stop_oracle", "maint_mode.sh"))
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class ConfigError(ValueError):
    pass


class ScriptConfig:
    def __init__(self, name, timeout):
        self.name = name
        self.file = ALLOWED_SCRIPTS[name]
        self.timeout = timeout
        self.state_changing = name in STATE_CHANGING
        self.description = "Run the allowlisted %s operation" % name


class AppConfig:
    def __init__(self, raw):
        allowed = {
            "jump_host", "jump_port", "known_hosts", "connect_timeout",
            "command_timeout", "max_concurrent", "max_output_bytes",
            "script_dir", "ccps_command", "ccps_secret_env", "api_token_env",
            "audit_log", "allow_insecure_http",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ConfigError("Unknown configuration key(s): %s" % ", ".join(sorted(unknown)))
        self.jump_host = raw.get("jump_host", JUMP_HOST)
        if self.jump_host != JUMP_HOST:
            raise ConfigError("jump_host must be %s" % JUMP_HOST)
        self.jump_port = _integer(raw, "jump_port", 22, 1, 65535)
        self.known_hosts = _string(raw, "known_hosts", "/opt/dbaops-api/known_hosts")
        self.connect_timeout = _integer(raw, "connect_timeout", 15, 1, 120)
        self.command_timeout = _integer(raw, "command_timeout", 600, 1, 3600)
        self.max_concurrent = _integer(raw, "max_concurrent", 4, 1, 32)
        self.max_output_bytes = _integer(raw, "max_output_bytes", 1048576, 1024, 10485760)
        self.script_dir = raw.get("script_dir", "/dba/bin")
        if self.script_dir != "/dba/bin":
            raise ConfigError("script_dir must be /dba/bin")
        self.ccps_command = raw.get("ccps_command", "ccps")
        if self.ccps_command != "ccps":
            raise ConfigError("ccps_command must be ccps")
        self.ccps_secret_env = _string(raw, "ccps_secret_env", "CCPS_SECRET")
        self.api_token_env = _string(raw, "api_token_env", "DBAOPS_API_TOKEN")
        if not ENV_NAME_RE.match(self.ccps_secret_env) or not ENV_NAME_RE.match(self.api_token_env):
            raise ConfigError("secret environment variable names are invalid")
        self.audit_log = raw.get("audit_log")
        if self.audit_log is not None and not isinstance(self.audit_log, str):
            raise ConfigError("audit_log must be a path string or null")
        self.allow_insecure_http = raw.get("allow_insecure_http", False)
        if not isinstance(self.allow_insecure_http, bool):
            raise ConfigError("allow_insecure_http must be true or false")
        self.scripts = dict((name, ScriptConfig(name, self.command_timeout))
                            for name in ALLOWED_SCRIPTS)

    @property
    def api_token(self):
        return os.environ.get(self.api_token_env)

    @property
    def ccps_secret(self):
        return os.environ.get(self.ccps_secret_env)

    def validate_secrets(self):
        if not self.api_token or len(self.api_token) < 32:
            raise ConfigError("%s must contain at least 32 characters" % self.api_token_env)
        if not self.ccps_secret:
            raise ConfigError("%s must be set" % self.ccps_secret_env)

    def validate_username(self, username):
        if not isinstance(username, str) or not USERNAME_RE.match(username):
            raise ConfigError("'username' must be a valid login name")
        return username

    def get_script(self, name):
        if not isinstance(name, str) or name not in self.scripts:
            raise ConfigError("Unknown script. Allowed: %s" % ", ".join(sorted(self.scripts)))
        return self.scripts[name]

    def script_path(self, script):
        return self.script_dir + "/" + script.file

    def timeout_for(self, script):
        return script.timeout


def _integer(raw, key, default, minimum, maximum):
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigError("%s must be an integer from %s to %s" % (key, minimum, maximum))
    return value


def _string(raw, key, default):
    value = raw.get(key, default)
    if not isinstance(value, str) or not value:
        raise ConfigError("%s must be a non-empty string" % key)
    return value


def load_config(path):
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"Config file not found: {p}")
    try:
        raw = json.loads(p.read_text())
    except ValueError as exc:
        raise ConfigError("%s is not valid JSON: %s" % (p, exc))
    if not isinstance(raw, dict):
        raise ConfigError("Config must be a JSON object")
    return AppConfig(dict((k, v) for k, v in raw.items() if not k.startswith("_")))
