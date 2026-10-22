from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Protocol

from .config import LlmConfig
from .redact import Redactor, truncate
from .ssh_runner import ScriptResult

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system.md").read_text()


class ChatModel(Protocol):
    def complete(self, system: str, user: str) -> str: ...


class OpenAIChatModel:
    """Works with OpenAI and any OpenAI-compatible endpoint (vLLM, Ollama, LiteLLM, ...) or Azure OpenAI."""

    def __init__(self, cfg: LlmConfig):
        if not cfg.api_key:
            raise RuntimeError(f"Set the {cfg.api_key_env} environment variable with your LLM API key")
        import openai

        if cfg.provider == "azure":
            if not cfg.base_url or not cfg.api_version:
                raise RuntimeError("Azure needs llm.base_url (endpoint) and llm.api_version")
            self._client = openai.AzureOpenAI(
                api_key=cfg.api_key, azure_endpoint=cfg.base_url,
                api_version=cfg.api_version, timeout=cfg.request_timeout,
            )
        else:
            self._client = openai.OpenAI(
                api_key=cfg.api_key, base_url=cfg.base_url, timeout=cfg.request_timeout,
            )
        self._cfg = cfg

    def complete(self, system: str, user: str) -> str:
        kwargs = {}
        if self._cfg.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        response = self._client.chat.completions.create(
            model=self._cfg.model,
            temperature=self._cfg.temperature,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            **kwargs,
        )
        return response.choices[0].message.content or ""


def build_user_prompt(results: list[ScriptResult], redactor: Redactor, max_chars: int) -> str:
    parts = [f"Health-check results from {len({r.server for r in results})} server(s).\n"]
    for r in results:
        parts.append(f"=== server: {r.server} ({r.host}) | script: {r.script} | status: {r.status} "
                     f"| exit_code: {r.exit_code} | duration: {r.duration_s:.1f}s ===")
        if r.error:
            parts.append(f"[runner error] {r.error}")
        parts.append("--- stdout ---")
        parts.append(truncate(redactor(r.stdout.strip()), max_chars) or "(empty)")
        if r.stderr.strip():
            parts.append("--- stderr ---")
            parts.append(truncate(redactor(r.stderr.strip()), max_chars // 4))
        parts.append("")
    parts.append("Produce the JSON action plan now.")
    return "\n".join(parts)


def parse_plan(text: str) -> dict:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start:end + 1]
    return json.loads(text)


def analyze(results: list[ScriptResult], model: ChatModel, cfg: LlmConfig, redactor: Redactor) -> dict:
    user = build_user_prompt(results, redactor, cfg.max_chars_per_output)
    raw = model.complete(SYSTEM_PROMPT, user)
    try:
        return parse_plan(raw)
    except (json.JSONDecodeError, ValueError):
        log.warning("LLM did not return valid JSON; asking it to fix the format")
        retry = model.complete(
            SYSTEM_PROMPT,
            user + "\n\nYour previous reply was not valid JSON. Reply with only the JSON object.",
        )
        try:
            return parse_plan(retry)
        except (json.JSONDecodeError, ValueError):
            return {"overall_status": "UNKNOWN", "summary": "LLM response was not valid JSON.",
                    "servers": [], "action_plan": [], "needs_human_review": [], "raw_response": retry}
