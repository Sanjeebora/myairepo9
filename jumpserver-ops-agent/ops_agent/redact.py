from __future__ import annotations

import re

_DEFAULT_PATTERNS = [
    # password=..., pwd: ..., secret = ...
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\b(\s*[:=]\s*)(\S+)",
    # Oracle: IDENTIFIED BY <pw>
    r"(?i)\b(identified\s+by)(\s+)(\S+)",
    # user/password@tns
    r"\b([A-Za-z0-9_$#]+)(/)([^\s@/]+)(?=@)",
]

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S
)


class Redactor:
    def __init__(self, extra_patterns: list[str] | None = None):
        self._patterns = [re.compile(p) for p in _DEFAULT_PATTERNS]
        self._extra = [re.compile(p) for p in extra_patterns or []]

    def __call__(self, text: str) -> str:
        if not text:
            return text
        text = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
        for pattern in self._patterns:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
        for pattern in self._extra:
            text = pattern.sub("[REDACTED]", text)
        return text


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    omitted = len(text) - limit
    return f"{text[:head]}\n... [{omitted} characters omitted] ...\n{text[-tail:]}"
