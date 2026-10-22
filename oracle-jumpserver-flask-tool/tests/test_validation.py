import pytest

from oracle_jump_tool.config import Settings
from oracle_jump_tool.scripts import ValidationError, parse_hosts, resolve_script, validate_host


@pytest.mark.parametrize("host", ["db01", "db01.example.com", "DB-02.corp.local", "10.1.2.3", "fe80::1", "db01.example.com."])
def test_valid_hosts(host):
    assert validate_host(host) == host.rstrip(".")


@pytest.mark.parametrize("host", [
    "", " ", "db01;id", "db01 && reboot", "$(id)", "`id`", "-oProxyCommand=sh", "db01|cat",
    "db01\nid", "a" * 300, "db_01", "db01/../x", "'db01'", "db01>x", "user@db01",
])
def test_rejected_hosts(host):
    with pytest.raises(ValidationError):
        validate_host(host)


def test_host_allow_regex_and_jump_host_block():
    assert validate_host("proddb01.corp.local", r".*\.corp\.local") == "proddb01.corp.local"
    with pytest.raises(ValidationError):
        validate_host("evil.example.com", r".*\.corp\.local")
    with pytest.raises(ValidationError):
        validate_host("JUMP.example.com", jump_host="jump.example.com")


@pytest.mark.parametrize("value,name", [
    ("showsid", "showsid"), ("/dba/bin/showsid", "showsid"), ("maint_mode", "maint_mode"),
    ("/dba/bin/maint_mode.sh -s", "maint_mode"), ("  /dba/bin/maint_mode.sh   -s ", "maint_mode"),
])
def test_resolve_script(value, name):
    assert resolve_script(value).name == name


@pytest.mark.parametrize("value", [None, "", "rm -rf /", "/dba/bin/showsid; id", "/dba/bin/maint_mode.sh",
                                   "/dba/bin/maint_mode.sh -x", "/bin/sh", 5])
def test_reject_scripts(value):
    with pytest.raises(ValidationError):
        resolve_script(value)


def test_parse_hosts():
    assert parse_hosts({"host": "db1", "hosts": ["db2", "DB1"]}, 5) == ["db1", "db2"]
    assert parse_hosts({"hosts": "db1, db2 db3"}, 5) == ["db1", "db2", "db3"]
    with pytest.raises(ValidationError):
        parse_hosts({}, 5)
    with pytest.raises(ValidationError):
        parse_hosts({"hosts": ["a", "b", "c"]}, 2)
    with pytest.raises(ValidationError):
        parse_hosts({"hosts": {"a": 1}}, 2)


def test_settings_from_env_overrides_ccps_password():
    assert Settings.from_env({}).ccps_password == "bake6Pies"
    assert Settings.from_env({"JT_CCPS_PASSWORD": "other"}).ccps_password == "other"
    assert Settings.from_env({}).bind_host == "127.0.0.1"
    with pytest.raises(ValueError):
        Settings.from_env({"JT_HOST_KEY_POLICY": "maybe"})
