"""How an LLM agent calls the tool. Standard library only.

    set JT_URL=http://127.0.0.1:5055
    set JT_API_KEY=<key printed by run.bat>
    python agent/example_client.py db01.example.com showsid

`TOOLS` is the OpenAI-style schema to pass as `tools=` to a chat-completions call;
`dispatch(name, arguments)` executes a tool call the model returns.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = os.environ.get("JT_URL", "http://127.0.0.1:5055").rstrip("/")
API_KEY = os.environ.get("JT_API_KEY", "")
TOOLS = json.loads((Path(__file__).resolve().parent.parent / "oracle_jump_tool" / "specs" / "tool_schema.json").read_text())


def _call(method: str, path: str, body: dict | None = None, timeout: float = 3700) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE_URL + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {API_KEY}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # 400/401/409/503 bodies are JSON the model can read: {"error": ..., "message": ...}
        return {"http_status": exc.code, **json.loads(exc.read() or b"{}")}


def dispatch(name: str, arguments: str | dict) -> dict:
    args = json.loads(arguments) if isinstance(arguments, str) else arguments
    if name == "run_oracle_script":
        body = {"script": args["script"], "hosts": args["hosts"]}
        if args.get("timeout_s"):
            body["timeout_s"] = args["timeout_s"]
        return _call("POST", "/run", body)
    if name == "list_oracle_scripts":
        return _call("GET", "/scripts")
    if name == "get_jump_session_status":
        return _call("GET", "/session")
    return {"error": "unknown_tool", "message": name}


def openai_loop(prompt: str, model: str = "gpt-4o") -> str:
    """Minimal tool-calling loop (pip install openai; set OPENAI_API_KEY)."""
    from openai import OpenAI

    client = OpenAI()
    messages = [{"role": "user", "content": prompt}]
    while True:
        reply = client.chat.completions.create(model=model, messages=messages, tools=TOOLS).choices[0].message
        messages.append(reply)
        if not reply.tool_calls:
            return reply.content
        for call in reply.tool_calls:
            result = dispatch(call.function.name, call.function.arguments)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)[:100_000]})


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    host, script = sys.argv[1], sys.argv[2]
    print(json.dumps(dispatch("run_oracle_script", {"script": script, "hosts": [host]}), indent=2))
