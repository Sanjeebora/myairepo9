import hmac
import json
import logging
import os
import threading
import time
from flask import Flask, jsonify, request

from . import __version__
from .config import ACTUAL_JUMP_HOST, ConfigError, load_config
from .executor import LocalExecutor
from .runner import Runner

log = logging.getLogger(__name__)
audit = logging.getLogger("dbaops.audit")


def default_config_path():
    return os.environ.get("DBAOPS_CONFIG", "config.json")


def _setup_audit(path):
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


def create_app(config_path=None, executor_factory=None, target_resolver=None):
    cfg = load_config(config_path or default_config_path())
    cfg.validate_secrets()
    runner = Runner(cfg)
    executor = (executor_factory or LocalExecutor)(cfg)
    slots = threading.BoundedSemaphore(cfg.max_concurrent)
    _setup_audit(cfg.audit_log)

    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False
    app.config["MAX_CONTENT_LENGTH"] = 16384

    def error(status, message, **extra):
        payload = {"error": message}
        payload.update(extra)
        return jsonify(payload), status

    def secure_transport():
        if request.is_secure:
            return True
        return (cfg.is_trusted_proxy(request.remote_addr) and
                request.headers.get("X-Forwarded-Proto", "").lower() == "https")

    @app.before_request
    def authenticate():
        if request.endpoint == "health" or request.endpoint is None:
            return None
        supplied = request.headers.get("X-Token", "")
        expected = cfg.api_token
        if not supplied or not hmac.compare_digest(
                supplied.encode("utf-8"), expected.encode("utf-8")):
            audit_event("authentication", result="rejected")
            return error(401, "Missing or invalid X-Token")
        return None

    @app.errorhandler(404)
    def not_found(_exc):
        return error(404, "Not found. Endpoints: GET /health, GET /scripts, POST /run")

    @app.errorhandler(405)
    def bad_method(_exc):
        return error(405, "Method not allowed")

    @app.errorhandler(500)
    def internal(_exc):
        return error(500, "Internal error; see the service log on the jump server")

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok", "version": __version__})

    @app.route("/scripts", methods=["GET"])
    def list_scripts():
        return jsonify({"scripts": [script_info(s) for s in cfg.scripts.values()]})

    @app.route("/run", methods=["POST"])
    def run():
        if not (secure_transport() or cfg.allow_insecure_http):
            return error(403, "Authenticated operations are accepted only over HTTPS")
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return error(400, "Body must be a JSON object with Content-Type: application/json")
        unknown = set(body) - {"target_host", "target_port", "script", "confirm"}
        if unknown:
            return error(400, "Unknown request field(s): %s" % ", ".join(sorted(unknown)))
        try:
            target = cfg.validate_target(
                body.get("target_host"), body.get("target_port"),
                resolver=target_resolver)
            script = cfg.get_script(body.get("script"))
        except ConfigError as exc:
            return error(400, str(exc))
        if "confirm" in body and not isinstance(body["confirm"], bool):
            return error(400, "'confirm' must be true or false")
        if script.state_changing and body.get("confirm") is not True:
            return error(409, "'%s' changes state; resend with confirm=true" % script.name,
                         script=script.name)

        if not slots.acquire(blocking=False):
            return error(429, "Maximum concurrent operations reached")
        try:
            result = runner.run(executor, target, script)
        finally:
            slots.release()

        audit_event("run", target_host=target.requested_host, target_port=target.port,
                    script=script.name, status=result.status,
                    exit_code=result.exit_code, duration_s=round(result.duration_s, 2),
                    confirm=bool(body.get("confirm")))

        payload = {
            "jump_host": ACTUAL_JUMP_HOST,
            "target_host": target.requested_host,
            "target_port": target.port,
            "script": script.name,
            "command": result.command,
            "exit_code": result.exit_code,
            "output": result.output,
            "stderr": result.stderr,
            "status": result.status,
            "duration_s": round(result.duration_s, 2),
        }
        if result.truncated:
            payload["truncated"] = True
        if result.infrastructure_error:
            payload["error"] = result.error
            return jsonify(payload), 502

        return jsonify(payload)

    def audit_event(event, **fields):
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event,
                  "remote_addr": request.remote_addr}
        record.update(fields)
        audit.info(json.dumps(record))
        log.info("audit event=%s result=%s", event, fields.get("status", fields.get("result")))

    def script_info(s) -> dict:
        return {"name": s.name, "description": s.description, "path": cfg.script_path(s),
                "requires_confirm": s.state_changing, "timeout": cfg.timeout_for(s)}

    app.extensions["dbaops"] = {"config": cfg, "runner": runner}
    return app
