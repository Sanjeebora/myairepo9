from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .config import AppConfig
from .llm import OpenAIChatModel, analyze
from .redact import Redactor
from .report import write_report
from .ssh_runner import JumpServerRunner, ParamikoExecutor, ScriptResult

log = logging.getLogger(__name__)


@dataclass
class RunOutcome:
    exit_code: int
    dry_run: bool
    commands: list[str] = field(default_factory=list)
    results: list[ScriptResult] = field(default_factory=list)
    plan: dict | None = None
    report_dir: Path | None = None


def preview_commands(cfg: AppConfig, script_names: list[str], server_names: list[str] | None,
                     tags: list[str] | None) -> list[str]:
    scripts = [cfg.get_script(name) for name in script_names]
    servers = cfg.select_servers(server_names, tags)
    runner = JumpServerRunner(cfg, executor=None)  # type: ignore[arg-type]
    lines: list[str] = []
    if cfg.ccps.mode == "once":
        lines.append(f"[jumpserver] {runner.wrap(cfg.ccps.command)}")
    for server in servers:
        for script in scripts:
            lines.append(f"[{server.name}] {runner.build_command(server, script)}")
    return lines


def execute_run(cfg: AppConfig, script_names: list[str], server_names: list[str] | None = None,
                tags: list[str] | None = None, *, dry_run: bool = False, no_llm: bool = False,
                executor_factory=None, model_factory=None) -> RunOutcome:
    """Run ccps and the allowlisted scripts, then ask the LLM for an action plan.

    `script_names` must already be in the config allowlist. Nothing else is executed.
    """
    commands = preview_commands(cfg, script_names, server_names, tags)
    if dry_run:
        return RunOutcome(exit_code=0, dry_run=True, commands=commands)

    scripts = [cfg.get_script(name) for name in script_names]
    servers = cfg.select_servers(server_names, tags)
    redactor = Redactor(cfg.redact_patterns)
    executor_factory = executor_factory or (lambda app_cfg: ParamikoExecutor(app_cfg))
    model_factory = model_factory or (lambda app_cfg: OpenAIChatModel(app_cfg.llm))
    model = None if no_llm else model_factory(cfg)

    with executor_factory(cfg) as executor:
        runner = JumpServerRunner(cfg, executor)
        if cfg.ccps.mode == "once":
            runner.run_ccps()
        results = runner.run_many(servers, scripts)

    for result in results:
        result.stdout = redactor(result.stdout)
        result.stderr = redactor(result.stderr)
        if result.error:
            result.error = redactor(result.error)

    plan = None
    if model is not None:
        log.info("Sending results to %s for analysis", cfg.llm.model)
        plan = analyze(results, model, cfg.llm, redactor)

    report_dir = write_report(cfg.reports_dir, plan, results, redactor)
    exit_code = 0 if all(result.status == "ok" for result in results) else 2
    return RunOutcome(exit_code=exit_code, dry_run=False, commands=commands, results=results,
                      plan=plan, report_dir=report_dir)
