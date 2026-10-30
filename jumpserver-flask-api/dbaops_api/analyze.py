from __future__ import annotations

import json
import urllib.request

from .config import LlmConfig

SYSTEM_PROMPT = """You are a senior Oracle DBA reviewing the output of a check script.
Reply with:
1. Summary: one or two sentences on overall health.
2. Findings: each problem with severity CRITICAL, HIGH, MEDIUM or LOW and the evidence line.
3. Action plan: ordered steps. Mark any step that changes state as REQUIRES APPROVAL.
Do not invent facts that are not in the output."""


def analyze(llm: LlmConfig, host: str, script: str, exit_code, output: str) -> str:
    """Call an OpenAI-compatible /chat/completions endpoint using only the standard library."""
    url = llm.base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": llm.model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"Host: {host}\nScript: {script}\nExit code: {exit_code}\n\n"
                f"Output:\n{output[-llm.max_chars:]}")},
        ],
    }
    headers = {"Content-Type": "application/json"}
    if llm.api_key:
        headers["Authorization"] = f"Bearer {llm.api_key}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=llm.timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]
