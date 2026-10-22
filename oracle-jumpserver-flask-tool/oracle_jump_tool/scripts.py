from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class AllowedScript:
    name: str
    argv: tuple[str, ...]
    description: str
    timeout: int | None = None

    @property
    def command(self) -> str:
        return " ".join(self.argv)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "command": self.command,
            "description": self.description,
            "timeout_s": self.timeout,
        }


# The only commands this tool will ever run on a target. Callers pick by name; nothing
# from the request is appended to the command line except the validated hostname.
ALLOWED_SCRIPTS: dict[str, AllowedScript] = {
    s.name: s
    for s in (
        AllowedScript("showsid", ("/dba/bin/showsid",), "List Oracle SIDs on the host and their status."),
        AllowedScript("showusers", ("/dba/bin/showusers",), "List database users and sessions."),
        AllowedScript("dba_checkout", ("/dba/bin/dba_checkout",), "Run the DBA health checkout.", 1800),
        AllowedScript("start_oracle", ("/dba/bin/start_oracle",), "Start Oracle instances and listeners on the host.", 1800),
        AllowedScript("maint_mode", ("/dba/bin/maint_mode.sh", "-s"), "Show maintenance-mode status (maint_mode.sh -s)."),
    )
}

_BY_COMMAND = {s.command: s for s in ALLOWED_SCRIPTS.values()}

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})*\.?$")


class ValidationError(ValueError):
    pass


def resolve_script(value: object) -> AllowedScript:
    """Accept an allowlisted name (`showsid`) or its exact command (`/dba/bin/maint_mode.sh -s`)."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("script is required")
    key = " ".join(value.split())
    script = ALLOWED_SCRIPTS.get(key) or _BY_COMMAND.get(key)
    if script is None:
        raise ValidationError(
            f"script {value!r} is not allowed; choose one of: {', '.join(ALLOWED_SCRIPTS)}"
        )
    return script


def validate_host(value: object, allowed_regex: str = "", jump_host: str = "") -> str:
    if not isinstance(value, str):
        raise ValidationError("host must be a string")
    host = value.strip()
    if not host:
        raise ValidationError("host is required")
    if not _is_ip(host) and not _HOSTNAME.fullmatch(host):
        raise ValidationError(f"host {value!r} is not a valid hostname or IP address")
    host = host.rstrip(".")
    if allowed_regex and not re.fullmatch(allowed_regex, host, flags=re.IGNORECASE):
        raise ValidationError(f"host {host!r} is not permitted by JT_ALLOWED_HOST_REGEX")
    if jump_host and host.lower() == jump_host.lower():
        raise ValidationError("host must be a database server, not the jump server")
    return host


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def parse_hosts(body: dict, max_hosts: int, allowed_regex: str = "", jump_host: str = "") -> list[str]:
    raw: list = []
    if "host" in body and body["host"] not in (None, ""):
        raw.append(body["host"])
    hosts_field = body.get("hosts")
    if isinstance(hosts_field, str):
        raw.extend(h for h in re.split(r"[\s,]+", hosts_field) if h)
    elif isinstance(hosts_field, list):
        raw.extend(hosts_field)
    elif hosts_field not in (None, ""):
        raise ValidationError("hosts must be a list of hostnames")
    if not raw:
        raise ValidationError("host or hosts is required")
    hosts: list[str] = []
    for item in raw:
        host = validate_host(item, allowed_regex, jump_host)
        if host.lower() not in (h.lower() for h in hosts):
            hosts.append(host)
    if len(hosts) > max_hosts:
        raise ValidationError(f"at most {max_hosts} hosts per request")
    return hosts
