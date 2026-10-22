from __future__ import annotations

import json
import logging
from functools import wraps
from importlib import resources
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, render_template, request

from . import __version__
from .config import Settings
from .scripts import ALLOWED_SCRIPTS, ValidationError, parse_hosts, resolve_script
from .session import Busy, LoginError, NotLoggedIn, SessionManager

log = logging.getLogger(__name__)

LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}


def _spec(name: str) -> dict:
    return json.loads(resources.files("oracle_jump_tool").joinpath("specs", name).read_text("utf-8"))


def error(status: int, code: str, message: str, **extra) -> tuple[Response, int]:
    return jsonify({"error": code, "message": message, **extra}), status


def create_app(settings: Settings, manager: SessionManager) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    app.extensions["jump_manager"] = manager

    def wants_html() -> bool:
        return request.mimetype in ("application/x-www-form-urlencoded", "multipart/form-data")

    def presented_key() -> str | None:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        if request.headers.get("X-API-Key"):
            return request.headers["X-API-Key"].strip()
        if wants_html():
            return request.form.get("api_key")
        return None

    def require_key(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not manager.check_key(presented_key()):
                return error(401, "unauthorized",
                             "Send the API key or session token as 'Authorization: Bearer <key>'")
            return view(*args, **kwargs)
        return wrapper

    @app.before_request
    def guard_host():
        if not settings.check_host_header or settings.bind_host not in LOOPBACK_NAMES:
            return None
        # Blocks DNS-rebinding pages from reaching this loopback-only service.
        host = (urlsplit("//" + (request.host or "")).hostname or "").lower()
        if host not in LOOPBACK_NAMES:
            return error(403, "forbidden_host", "Use http://127.0.0.1 or http://localhost")
        origin = request.headers.get("Origin")
        if origin and request.method != "GET":
            if (urlsplit(origin).hostname or "").lower() not in LOOPBACK_NAMES:
                return error(403, "forbidden_origin", "Cross-site requests are not allowed")
        return None

    @app.after_request
    def no_store(resp: Response) -> Response:
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    @app.errorhandler(404)
    def not_found(_):
        return error(404, "not_found", "No such endpoint")

    @app.errorhandler(405)
    def bad_method(_):
        return error(405, "method_not_allowed", "Method not allowed")

    @app.errorhandler(413)
    def too_large(_):
        return error(413, "too_large", "Request body too large")

    @app.get("/health")
    def health():
        session = manager.current()
        return jsonify({
            "status": "ok",
            "version": __version__,
            "logged_in": session is not None,
            "jump_host": session.jump_host if session else (settings.jump_host or None),
        })

    @app.get("/")
    def index():
        session = manager.current()
        return render_template("login.html", settings=settings, session=session,
                               scripts=ALLOWED_SCRIPTS.values(), result=None)

    @app.post("/login")
    def login():
        body = request.form if wants_html() else (request.get_json(silent=True) or {})
        port = body.get("jump_port")
        try:
            port = int(port) if port not in (None, "") else None
            session = manager.login(body.get("username", ""), body.get("password", ""),
                                    body.get("jump_host") or None, port)
        except (TypeError, ValueError):
            return error(400, "bad_request", "jump_port must be an integer")
        except LoginError as exc:
            log.warning("Login failed at %s: %s", exc.stage, exc)
            if wants_html():
                return render_template("login.html", settings=settings, session=None,
                                       scripts=ALLOWED_SCRIPTS.values(),
                                       result={"error": str(exc), "transcript": exc.transcript}), exc.status
            return error(exc.status, "login_failed", str(exc), stage=exc.stage, transcript=exc.transcript)
        info = session.to_dict(settings)
        if wants_html():
            return render_template("login.html", settings=settings, session=session,
                                   scripts=ALLOWED_SCRIPTS.values(),
                                   result={"token": session.token, "api_key": manager.api_key})
        return jsonify({"token": session.token, "token_type": "Bearer", "session": info})

    @app.post("/logout")
    @require_key
    def logout():
        ended = manager.logout()
        if wants_html():
            return render_template("login.html", settings=settings, session=None,
                                   scripts=ALLOWED_SCRIPTS.values(), result={"logged_out": True})
        return jsonify({"logged_out": ended})

    @app.get("/session")
    @require_key
    def session_info():
        session = manager.current()
        if session is None:
            return error(409, "not_logged_in", _login_hint())
        return jsonify({"logged_in": True, "session": session.to_dict(settings)})

    @app.get("/scripts")
    def scripts():
        return jsonify({"scripts": [s.to_dict() for s in ALLOWED_SCRIPTS.values()]})

    @app.post("/run")
    @require_key
    def run():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return error(400, "bad_request", "Send a JSON object: {\"host\": \"...\", \"script\": \"showsid\"}")
        try:
            script = resolve_script(body.get("script"))
            session = manager.current()
            hosts = parse_hosts(body, settings.max_hosts, settings.allowed_host_regex,
                                session.jump_host if session else settings.jump_host)
            timeout = _timeout(body.get("timeout_s"), script.timeout or settings.command_timeout,
                               settings.max_command_timeout)
        except ValidationError as exc:
            return error(400, "invalid_request", str(exc))
        try:
            results, closed = manager.run(hosts, script, timeout)
        except NotLoggedIn:
            return error(409, "not_logged_in", _login_hint())
        except Busy:
            resp, status = error(503, "busy", "Another script is still running; retry later")
            resp.headers["Retry-After"] = "30"
            return resp, status
        return jsonify({
            "ok": all(r.status == "ok" for r in results),
            "script": script.name,
            "command": script.command,
            "results": [r.to_dict() for r in results],
            "session_closed": closed,
        })

    @app.get("/openapi.json")
    def openapi():
        spec = _spec("openapi.json")
        spec["servers"] = [{"url": request.host_url.rstrip("/")}]
        return jsonify(spec)

    @app.get("/tool-schema.json")
    def tool_schema():
        return jsonify(_spec("tool_schema.json"))

    def _login_hint() -> str:
        return (f"No jump server session. Ask the user to log in at "
                f"http://{settings.bind_host}:{settings.bind_port}/ or in the tool's console.")

    return app


def _timeout(raw, default: int, maximum: int) -> int:
    if raw in (None, ""):
        return min(default, maximum)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError("timeout_s must be an integer number of seconds") from exc
    if not 5 <= value <= maximum:
        raise ValidationError(f"timeout_s must be between 5 and {maximum}")
    return value
