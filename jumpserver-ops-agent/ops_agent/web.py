from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from .config import ConfigError, load_config
from .runner import execute_run, preview_commands

log = logging.getLogger(__name__)

_REPORT_ID = re.compile(r"^\d{8}-\d{6}$")
_RAW_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,200}$")


class Job:
    def __init__(self, job_id: str, label: str):
        self.id = job_id
        self.label = label
        self.status = "running"
        self.error: str | None = None
        self.outcome = None
        self.thread: threading.Thread | None = None


def create_app(config_path: str | Path = "config.yaml", executor_factory=None,
               model_factory=None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["CONFIG_PATH"] = str(config_path)
    app.config["EXECUTOR_FACTORY"] = executor_factory
    app.config["MODEL_FACTORY"] = model_factory
    app.extensions["jobs"] = {}
    app.extensions["jobs_lock"] = threading.Lock()

    def cfg_or_none():
        try:
            return load_config(app.config["CONFIG_PATH"]), None
        except ConfigError as exc:
            return None, str(exc)

    @app.get("/")
    def index():
        cfg, error = cfg_or_none()
        return render_template("index.html", cfg=cfg, error=error)

    @app.post("/runs")
    def start_run():
        cfg, error = cfg_or_none()
        if error:
            return render_template("index.html", cfg=None, error=error), 400
        script_names = [name for name in request.form.getlist("script") if name.strip()]
        server_names = [name for name in request.form.getlist("server") if name.strip()] or None
        dry_run = request.form.get("dry_run") == "1"
        no_llm = request.form.get("no_llm") == "1"
        if not script_names:
            return render_template("index.html", cfg=cfg, error="Select at least one script."), 400
        try:
            preview_commands(cfg, script_names, server_names, None)
        except ConfigError as exc:
            return render_template("index.html", cfg=cfg, error=str(exc)), 400

        job = Job(uuid.uuid4().hex[:12], ", ".join(script_names))

        def work() -> None:
            try:
                kwargs = {}
                if app.config["EXECUTOR_FACTORY"] is not None:
                    kwargs["executor_factory"] = app.config["EXECUTOR_FACTORY"]
                if app.config["MODEL_FACTORY"] is not None:
                    kwargs["model_factory"] = app.config["MODEL_FACTORY"]
                job.outcome = execute_run(
                    cfg, script_names, server_names, None,
                    dry_run=dry_run, no_llm=no_llm or dry_run, **kwargs,
                )
                job.status = "done"
            except Exception as exc:  # noqa: BLE001 - shown on the run page
                log.exception("Run %s failed", job.id)
                job.error = f"{type(exc).__name__}: {exc}"
                job.status = "error"

        job.thread = threading.Thread(target=work, name=f"run-{job.id}", daemon=True)
        with app.extensions["jobs_lock"]:
            app.extensions["jobs"][job.id] = job
        job.thread.start()
        return redirect(url_for("show_run", job_id=job.id))

    def _job(job_id: str) -> Job:
        with app.extensions["jobs_lock"]:
            job = app.extensions["jobs"].get(job_id)
        if job is None:
            abort(404)
        return job

    @app.get("/runs/<job_id>")
    def show_run(job_id: str):
        return render_template("run.html", job=_job(job_id))

    @app.get("/api/runs/<job_id>")
    def run_status(job_id: str):
        job = _job(job_id)
        outcome = job.outcome
        return jsonify({
            "id": job.id,
            "status": job.status,
            "error": job.error,
            "exit_code": None if outcome is None else outcome.exit_code,
            "dry_run": None if outcome is None else outcome.dry_run,
        })

    @app.get("/reports")
    def reports():
        cfg, error = cfg_or_none()
        found = []
        if cfg is not None:
            root = Path(cfg.reports_dir)
            if root.is_dir():
                for report_json in sorted(root.glob("*/report.json"), reverse=True):
                    if _REPORT_ID.match(report_json.parent.name):
                        found.append(_read_report(report_json))
        return render_template("reports.html", reports=found, error=error)

    @app.get("/reports/<report_id>")
    def show_report(report_id: str):
        report_json = _report_file(report_id)
        if report_json is None:
            abort(404)
        data = _read_report(report_json)
        raw_dir = report_json.parent / "raw"
        logs = sorted(p.name for p in raw_dir.glob("*.log")) if raw_dir.is_dir() else []
        return render_template("report.html", report=data, logs=logs)

    @app.get("/reports/<report_id>/raw/<name>")
    def raw_log(report_id: str, name: str):
        report_json = _report_file(report_id)
        if report_json is None or not _RAW_NAME.match(name):
            abort(404)
        path = (report_json.parent / "raw" / name).resolve()
        raw_root = (report_json.parent / "raw").resolve()
        if path.parent != raw_root or not path.is_file():
            abort(404)
        return path.read_text(errors="replace"), 200, {"Content-Type": "text/plain; charset=utf-8"}

    def _report_file(report_id: str) -> Path | None:
        if not _REPORT_ID.match(report_id):
            return None
        cfg, error = cfg_or_none()
        if error:
            return None
        path = Path(cfg.reports_dir) / report_id / "report.json"
        return path if path.is_file() else None

    return app


def _read_report(path: Path) -> dict:
    data = json.loads(path.read_text())
    data["id"] = path.parent.name
    return data


def serve(config_path: str = "config.yaml", host: str = "127.0.0.1", port: int = 5000) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        log.warning("Binding to %s. This app runs scripts on your servers; prefer 127.0.0.1.", host)
    app = create_app(config_path)
    print(f"Open http://{host}:{port}")
    print("Only this computer can open the page when host is 127.0.0.1. Press Ctrl+C to stop.")
    app.run(host=host, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    serve()
