from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import os
import pwd
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence


KEY_TYPES = {
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
}


class HostKeyError(RuntimeError):
    pass


@dataclass(frozen=True)
class EnrollmentResult:
    status: str  # known | enrolled
    host: str
    known_hosts: str
    fingerprints: List[str]


def default_known_hosts() -> Path:
    """Resolve the effective account's home without trusting an inherited HOME."""
    return Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".ssh" / "known_hosts"


def sha256_fingerprint(key_data: str) -> str:
    try:
        blob = base64.b64decode(key_data.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise HostKeyError("ssh-keyscan returned malformed key data") from exc
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return "SHA256:" + digest


def _matching_lines(output: str, host: str, expected: Iterable[str], require_host: bool = True):
    expected_set = set(expected)
    observed = []
    matching = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if (len(fields) < 3 or (require_host and host not in fields[0].split(","))
                or fields[1] not in KEY_TYPES):
            continue
        fingerprint = sha256_fingerprint(fields[2])
        observed.append(fingerprint)
        if fingerprint in expected_set:
            matching.append((line, fingerprint))
    return matching, sorted(set(observed))


def enroll_host(
    host: str,
    expected_fingerprints: Sequence[str],
    known_hosts: Optional[Path] = None,
    timeout: int = 10,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> EnrollmentResult:
    """Enroll only a key matching an independently obtained SHA-256 fingerprint.

    Existing entries are never replaced. Runtime OpenSSH remains responsible for rejecting a
    changed key with StrictHostKeyChecking=yes.
    """
    if not expected_fingerprints:
        raise HostKeyError(f"No trusted fingerprints are configured for {host}")
    path = Path(known_hosts) if known_hosts else default_known_hosts()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(str(path.parent), 0o700)

    flags = os.O_CREAT | os.O_RDWR | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(path), flags, 0o600)
    except OSError as exc:
        raise HostKeyError(f"Cannot safely open {path}: {exc}") from exc

    with os.fdopen(fd, "a+", encoding="utf-8") as known_file:
        os.fchmod(known_file.fileno(), 0o600)
        fcntl.flock(known_file.fileno(), fcntl.LOCK_EX)
        lookup = run(
            ["ssh-keygen", "-F", host, "-f", str(path)],
            text=True, capture_output=True, check=False)
        if lookup.returncode == 0 and lookup.stdout.strip():
            matching, observed = _matching_lines(
                lookup.stdout, host, expected_fingerprints, require_host=False)
            if not matching:
                detail = ", ".join(observed) if observed else "no usable key found"
                raise HostKeyError(
                    f"Refusing existing entry for {host}: known_hosts does not match a trusted "
                    f"fingerprint (observed: {detail})")
            return EnrollmentResult(
                "known", host, str(path),
                sorted({fingerprint for _, fingerprint in matching}))
        if lookup.returncode not in {0, 1}:
            raise HostKeyError(f"Could not inspect {path}: {lookup.stderr.strip()}")

        scan = run(
            ["ssh-keyscan", "-T", str(timeout), "-t", "rsa,ecdsa,ed25519", host],
            text=True, capture_output=True, check=False)
        matching, observed = _matching_lines(scan.stdout, host, expected_fingerprints)
        if not matching:
            detail = ", ".join(observed) if observed else "no usable key returned"
            raise HostKeyError(
                f"Refusing to enroll {host}: scanned key did not match a trusted fingerprint "
                f"(observed: {detail})")

        known_file.seek(0, os.SEEK_END)
        if known_file.tell() and not _ends_with_newline(path):
            known_file.write("\n")
        for line, _fingerprint in matching:
            known_file.write(line + "\n")
        known_file.flush()
        os.fsync(known_file.fileno())

    return EnrollmentResult(
        "enrolled", host, str(path), sorted({fingerprint for _, fingerprint in matching}))


def _ends_with_newline(path: Path) -> bool:
    with path.open("rb") as stream:
        stream.seek(-1, os.SEEK_END)
        return stream.read(1) == b"\n"
