from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Optional

from flask import Flask, g, jsonify, request

from . import __version__
from .analyze import analyze as llm_analyze
from .config import AppConfig, ConfigError, load_config
from .runner import Runner
from .sessions import AuthError, JumpUnavailable, LoginFailed, SessionStore, TooManyFailures

log = logging.getLogger(__name__)
audit = logging.getLogger("dbaops.audit")

PUBLIC_ENDPOINTS = {"health", "login"}
LOOPBACK = {"127.0.0.1", "::1"}


def default_config_path() -> str:
    return os.environ.get("DBAOPS_CONFIG", "config.json")


def _setup_audit(path: Optional[str]) -> None:
    if not path:
        return
    path = os.path.abspath(path)
    for existing in list(audit.handlers):
        if isinstance(existing, logging.FileHandler):
            if existing.baseFilename == path:
                return
            audit.removeHandler(existing)
            existing.close()
    handler = logging.FileHandler(path)
    handler.setFormatter(logging.Formatter("%(message)s"))
    audit.addHandler(handler)
    audit.setLevel(logging.INFO)
    audit.propagate = False


def _default_connector(cfg, username, password):
    from .jumphost import JumpSession
    return JumpSession(cfg, username, password)


def create_app(config_path: Optional[str] = None,
               connector: Optional[Callable[..., Any]] = None,
               analyzer: Optional[Callable[..., str]] = None) -> Flask:
    cfg: AppConfig = load_config(config_path or default_config_path())
    store = SessionStore(cfg.jumpserver, cfg.session_ttl_seconds, connector or _default_connector)
    atexit.register(store.close_all)
    runner = Runner(cfg)
    analyzer = analyzer or llm_analyze
    slots = threading.BoundedSemaphore(cfg.max_concurrent)
    _setup_audit(cfg.audit_log)

    app = Flask(__name__)
    app.json.sort_keys = False  # keep host, script, command, exit_code, output in order

    def error(status: int, message: str, **extra: Any):
        return jsonify({"error": message, **extra}), status

    def session_token() -> Optional[str]:
        token = request.headers.get("X-Session-Token")
        auth = request.headers.get("Authorization", "")
        if not token and auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        return token

    @app.before_request
    def require_session():
        if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
            return None
        try:
            g.session = store.get(session_token())
        except AuthError as exc:
            return error(401, str(exc))
        g.user = g.session.username
        return None

    @app.errorhandler(404)
    def not_found(_exc):
        return error(404, "Not found. Endpoints: GET /health, POST|GET|DELETE /session, "
                          "GET /scripts, GET /tools, POST /run")

    @app.errorhandler(405)
    def bad_method(_exc):
        return error(405, "Method not allowed")

    @app.errorhandler(500)
    def internal(_exc):
        return error(500, "Internal error; see the service log on the jump server")

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "version": __version__})

    @app.post("/session")
    def login():
        if not (cfg.jumpserver.allow_http_login or request.is_secure
                or request.remote_addr in LOOPBACK):
            return error(403, "Refusing to accept a password over plain HTTP. Use HTTPS "
                              "(see README) or set jumpserver.allow_http_login.")
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return error(400, 'Body must be JSON: {"username": "...", "password": "..."}')
        password = body.get("password")
        try:
            username = cfg.validate_username(body.get("username"))
        except ConfigError as exc:
            return error(400, str(exc))
        if not isinstance(password, str) or not password:
            return error(400, "'password' is required")
        g.user = username
        try:
            session = store.login(username, password, request.remote_addr or "")
        except TooManyFailures as exc:
            audit_event("login", result="rate_limited")
            return error(429, str(exc))
        except LoginFailed:
            audit_event("login", result="failed")
            return error(401, "Invalid username or password for the jump server")
        except JumpUnavailable as exc:
            audit_event("login", result="jump_unavailable")
            return error(502, str(exc))
        audit_event("login", result="ok")
        return jsonify(session_info(session, token=True))

    @app.get("/session")
    def whoami():
        return jsonify(session_info(g.session))

    @app.delete("/session")
    def logout():
        store.logout(g.session.token)
        audit_event("logout")
        return jsonify({"logged_out": True, "user": g.user})

    @app.get("/scripts")
    def list_scripts():
        return jsonify({"scripts": [script_info(s) for s in cfg.scripts.values()]})

    @app.get("/tools")
    def tools():
        return jsonify({"tools": [tool_definition(cfg)]})

    @app.post("/run")
    def run():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return error(400, "Body must be a JSON object with Content-Type: application/json")
        try:
            host = cfg.validate_host(body.get("host"))
            script = cfg.get_script(body.get("script"))
        except ConfigError as exc:
            return error(400, str(exc))
        for flag in ("confirm", "analyze", "dry_run"):
            if flag in body and not isinstance(body[flag], bool):
                return error(400, f"'{flag}' must be true or false")
        if script.state_changing and body.get("confirm") is not True:
            return error(409, f"'{script.name}' changes state on {host}. "
                              "Resend with \"confirm\": true to run it.",
                         host=host, script=script.name)
        if body.get("analyze") and not cfg.llm.enabled:
            return error(400, "analyze=true but no LLM is configured (llm.base_url, llm.model)")

        if body.get("dry_run"):
            return jsonify({"host": host, "script": script.name, "user": g.user,
                            "command": runner.ssh_command(host, script),
                            "jump_command": runner.argv(host, script), "dry_run": True})

        if not slots.acquire(blocking=False):
            return error(429, f"Busy: {cfg.max_concurrent} scripts already running. Retry shortly.")
        try:
            result = runner.run(g.session.conn, host, script)
        finally:
            slots.release()

        audit_event("run", host=host, script=script.name, status=result.status,
                    exit_code=result.exit_code, duration_s=round(result.duration_s, 2),
                    confirm=bool(body.get("confirm")))

        payload = {
            "host": host,
            "script": script.name,
            "command": result.command,
            "exit_code": result.exit_code,
            "output": result.output,
            "stderr": result.stderr,
            "status": result.status,
            "user": g.user,
            "duration_s": round(result.duration_s, 2),
        }
        if result.truncated:
            payload["truncated"] = True
        if result.infrastructure_error:
            payload["error"] = result.error
            return jsonify(payload), 502

        if body.get("analyze"):
            try:
                payload["analysis"] = analyzer(cfg.llm, host, script.name,
                                               result.exit_code, result.output)
            except Exception as exc:  # noqa: BLE001 - the script output is still useful
                log.warning("LLM analysis failed: %s", exc)
                payload["analysis_error"] = f"{type(exc).__name__}: {exc}"
        return jsonify(payload)

    def session_info(session, token: bool = False) -> dict:
        info = {"user": session.username,
                "expires_at": session.expires_at(store.ttl),
                "idle_expires_at": session.idle_expires_at(store.idle),
                "idle_timeout": store.idle}
        if token:
            info = {"token": session.token, **info}
        return info

    def audit_event(event: str, **fields: Any) -> None:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event,
                  "user": getattr(g, "user", None),
                  "remote_addr": request.remote_addr, **fields}
        audit.info(json.dumps(record))
        log.info("%s", record)

    def script_info(s) -> dict:
        return {"name": s.name, "description": s.description, "path": cfg.script_path(s),
                "args": s.args, "state_changing": s.state_changing,
                "requires_confirm": s.state_changing, "timeout": cfg.timeout_for(s)}

    app.extensions["dbaops"] = {"config": cfg, "runner": runner, "sessions": store}
    return app


def tool_definition(cfg: AppConfig) -> dict:
    """An OpenAI-style function tool the agent harness can hand straight to its LLM."""
    lines = [f"- {s.name}: {s.description or s.file}"
             + (" (STATE-CHANGING: needs confirm=true and human approval)" if s.state_changing else "")
             for s in cfg.scripts.values()]
    return {
        "type": "function",
        "function": {
            "name": "run_dba_script",
            "description": ("Run an allow-listed script from /dba/bin on an Oracle database server "
                            "through the jump server and return its raw output.\nScripts:\n"
                            + "\n".join(lines)),
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Target DB server hostname",
                             "pattern": cfg.host_pattern},
                    "script": {"type": "string", "enum": sorted(cfg.scripts)},
                    "confirm": {"type": "boolean", "default": False,
                                "description": "Must be true for state-changing scripts"},
                },
                "required": ["host", "script"],
                "additionalProperties": False,
            },
        },
    }
