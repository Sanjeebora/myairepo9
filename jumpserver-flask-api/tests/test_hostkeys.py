import base64
import stat
from types import SimpleNamespace

import pytest

from dbaops_api.hostkeys import HostKeyError, enroll_host, sha256_fingerprint


def key_line(host, blob=b"mock-host-public-key"):
    encoded = base64.b64encode(blob).decode()
    return f"{host} ssh-ed25519 {encoded}"


def completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_first_connection_enrolls_only_out_of_band_verified_key(tmp_path):
    host = "dbnew01"
    line = key_line(host)
    fingerprint = sha256_fingerprint(line.split()[2])
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if argv[0] == "ssh-keygen":
            return completed(returncode=1)
        return completed(stdout=f"# scan banner\n{line}\n")

    path = tmp_path / ".ssh" / "known_hosts"
    result = enroll_host(host, [fingerprint], path, run=run)

    assert result.status == "enrolled"
    assert result.fingerprints == [fingerprint]
    assert path.read_text() == line + "\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [call[0] for call in calls] == ["ssh-keygen", "ssh-keyscan"]


def test_known_host_is_left_unchanged_and_not_rescanned(tmp_path):
    host = "dbknown01"
    line = key_line(host)
    fingerprint = sha256_fingerprint(line.split()[2])
    path = tmp_path / "known_hosts"
    path.write_text(line + "\n")
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return completed(stdout=f"# Host {host} found\n{line}\n")

    result = enroll_host(host, [fingerprint], path, run=run)

    assert result.status == "known"
    assert result.fingerprints == [fingerprint]
    assert path.read_text() == line + "\n"
    assert [call[0] for call in calls] == ["ssh-keygen"]


def test_first_connection_rejects_scanned_key_mismatch(tmp_path):
    host = "dbnew01"
    scanned = key_line(host, b"untrusted-key")

    def run(argv, **_kwargs):
        if argv[0] == "ssh-keygen":
            return completed(returncode=1)
        return completed(stdout=scanned + "\n")

    path = tmp_path / "known_hosts"
    with pytest.raises(HostKeyError, match="did not match a trusted fingerprint"):
        enroll_host(host, ["SHA256:" + "A" * 43], path, run=run)

    assert path.read_text() == ""


def test_existing_known_host_fingerprint_mismatch_is_not_replaced(tmp_path):
    host = "dbchanged01"
    line = key_line(host, b"old-or-changed-key")
    path = tmp_path / "known_hosts"
    path.write_text(line + "\n")
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return completed(stdout=f"# Host {host} found\n{line}\n")

    with pytest.raises(HostKeyError, match="existing entry"):
        enroll_host(host, ["SHA256:" + "A" * 43], path, run=run)

    assert path.read_text() == line + "\n"
    assert [call[0] for call in calls] == ["ssh-keygen"]
