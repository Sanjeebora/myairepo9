import json
import ipaddress
import os
import re
import socket
from pathlib import Path

ACTUAL_JUMP_HOST = "10.17.66.19"
BLOCKED_SSH_ADDRESSES = frozenset((
    ipaddress.ip_address("10.17.34.78"),
    ipaddress.ip_address("10.17.66.19"),
))
ALLOWED_SCRIPTS = {
    "dba_checkout": "dba_checkout",
    "start_oracle": "start_oracle",
    "stop_oracle": "stop_oracle",
    "maint_mode.sh": "maint_mode.sh",
}
STATE_CHANGING = frozenset(("start_oracle", "stop_oracle", "maint_mode.sh"))
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
HOSTNAME_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9][A-Za-z0-9-]{0,62})*$")


class ConfigError(ValueError):
    pass


class ScriptConfig:
    def __init__(self, name, timeout):
        self.name = name
        self.file = ALLOWED_SCRIPTS[name]
        self.timeout = timeout
        self.state_changing = name in STATE_CHANGING
        self.description = "Run the allowlisted %s operation" % name


class Target:
    def __init__(self, requested_host, connect_host, port, host_key_alias=None):
        self.requested_host = requested_host
        self.connect_host = connect_host
        self.port = port
        self.host_key_alias = host_key_alias


class AppConfig:
    def __init__(self, raw):
        allowed = {
            "actual_jump_host", "target_known_hosts", "target_connect_timeout",
            "target_allowed_hosts", "target_allowed_cidrs", "target_allowed_ports",
            "command_timeout", "max_concurrent", "max_output_bytes",
            "script_dir", "ccps_command", "ccps_secret_env", "api_token_env",
            "audit_log", "allow_insecure_http", "trusted_proxy_cidrs",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ConfigError("Unknown configuration key(s): %s" % ", ".join(sorted(unknown)))
        self.actual_jump_host = raw.get("actual_jump_host", ACTUAL_JUMP_HOST)
        if self.actual_jump_host != ACTUAL_JUMP_HOST:
            raise ConfigError("actual_jump_host must be %s" % ACTUAL_JUMP_HOST)
        self.target_known_hosts = _string(
            raw, "target_known_hosts", "/etc/dbaops-api/target_known_hosts")
        self.target_connect_timeout = _integer(
            raw, "target_connect_timeout", 10, 1, 120)
        self.target_allowed_hosts = _string_list(
            raw, "target_allowed_hosts", [])
        self.target_allowed_ports = _integer_list(
            raw, "target_allowed_ports", [22], 1, 65535)
        cidrs = _string_list(raw, "target_allowed_cidrs", [])
        try:
            self.target_allowed_cidrs = [ipaddress.ip_network(value) for value in cidrs]
        except ValueError as exc:
            raise ConfigError("target_allowed_cidrs contains an invalid network: %s" % exc)
        if not self.target_allowed_hosts and not self.target_allowed_cidrs:
            raise ConfigError(
                "At least one target_allowed_hosts or target_allowed_cidrs entry is required")
        for host in self.target_allowed_hosts:
            _validate_host_syntax(host)
        # Gunicorn is configured for 660s; leave room for the executor's 30s
        # backstop and 10s process-group cleanup.
        self.command_timeout = _integer(raw, "command_timeout", 600, 1, 600)
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
        proxy_cidrs = _string_list(raw, "trusted_proxy_cidrs", [])
        try:
            self.trusted_proxy_cidrs = [
                ipaddress.ip_network(value) for value in proxy_cidrs]
        except ValueError as exc:
            raise ConfigError("trusted_proxy_cidrs contains an invalid network: %s" % exc)
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

    def get_script(self, name):
        if not isinstance(name, str) or name not in self.scripts:
            raise ConfigError("Unknown script. Allowed: %s" % ", ".join(sorted(self.scripts)))
        return self.scripts[name]

    def script_path(self, script):
        return self.script_dir + "/" + script.file

    def timeout_for(self, script):
        return script.timeout

    def validate_target(self, host, port, resolver=None):
        host = _validate_host_syntax(host)
        if isinstance(port, bool) or not isinstance(port, int):
            raise ConfigError("'target_port' must be an integer")
        if port not in self.target_allowed_ports:
            raise ConfigError("Target port %s is not allowed" % port)

        exact = host.lower() in set(
            value.lower() for value in self.target_allowed_hosts)
        address = _ip_address(host)
        if address is not None:
            self._reject_jump_server(address)
            if exact or self._address_allowed(address):
                return Target(host, host, port)
            raise ConfigError("Target address is outside the configured allowlist")

        if not exact and not self.target_allowed_cidrs:
            raise ConfigError("Target host is not in target_allowed_hosts")
        resolver = resolver or socket.getaddrinfo
        try:
            records = resolver(host, port, 0, socket.SOCK_STREAM)
            addresses = sorted(set(record[4][0] for record in records))
        except (socket.gaierror, OSError):
            raise ConfigError("Target host could not be resolved")
        if not addresses:
            raise ConfigError("Target host could not be resolved")
        parsed = [_ip_address(value) for value in addresses]
        if any(value is None for value in parsed):
            raise ConfigError("Target host resolved to an invalid address")
        for value in parsed:
            self._reject_jump_server(value)
        if not exact and any(
                not self._address_allowed(value) for value in parsed):
            raise ConfigError("Target host resolves outside the configured CIDR allowlist")
        # Connect to the validated numeric address so DNS cannot change between policy and SSH.
        return Target(
            host, addresses[0], port, host_key_alias=host if exact else None)

    def _address_allowed(self, address):
        return any(address in network for network in self.target_allowed_cidrs)

    def _reject_jump_server(self, address):
        if address in BLOCKED_SSH_ADDRESSES:
            raise ConfigError("The jump-server VIP and actual host cannot be SSH targets")

    def is_trusted_proxy(self, address):
        parsed = _ip_address(address)
        return parsed is not None and any(
            parsed in network for network in self.trusted_proxy_cidrs)


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


def _string_list(raw, key, default):
    value = raw.get(key, default)
    if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value):
        raise ConfigError("%s must be a list of non-empty strings" % key)
    return value


def _integer_list(raw, key, default, minimum, maximum):
    value = raw.get(key, default)
    if not isinstance(value, list) or not value:
        raise ConfigError("%s must be a non-empty list" % key)
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum:
            raise ConfigError("%s contains an invalid port" % key)
    return value


def _validate_host_syntax(host):
    if not isinstance(host, str) or not host or len(host) > 253:
        raise ConfigError("'target_host' must be a valid hostname or IP address")
    if _ip_address(host) is None and not HOSTNAME_RE.match(host):
        raise ConfigError("'target_host' must be a valid hostname or IP address")
    return host


def _ip_address(value):
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


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
