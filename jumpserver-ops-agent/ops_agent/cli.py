from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

from .config import ConfigError, load_config
from .llm import OpenAIChatModel, analyze
from .redact import Redactor
from .report import write_report
from .ssh_runner import JumpServerRunner, ParamikoExecutor


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ops_agent",
        description="Run predefined scripts on servers via a jumpserver and get an LLM action plan.",
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config YAML")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="Validate config and list servers and allowed scripts")

    run = sub.add_parser("run", help="Connect, run ccps, run scripts, analyze with the LLM")
    run.add_argument("-s", "--script", action="append", required=True,
                     help="Allowlisted script name (repeatable), e.g. dba_checkout")
    run.add_argument("--servers", type=_csv, help="Comma-separated server names (default: all)")
    run.add_argument("--tags", type=_csv, help="Comma-separated tags to select servers")
    run.add_argument("--no-llm", action="store_true", help="Only collect output, skip the LLM")
    run.add_argument("--dry-run", action="store_true",
                     help="Print the commands that would run on the jumpserver and exit")
    return parser


def cmd_list(cfg) -> int:
    print(f"Jumpserver: {cfg.jumpserver.username}@{cfg.jumpserver.host}:{cfg.jumpserver.port}")
    print(f"ccps: '{cfg.ccps.command}' (mode={cfg.ccps.mode})")
    print(f"LLM: {cfg.llm.provider} model={cfg.llm.model} "
          f"key_env={cfg.llm.api_key_env} ({'set' if cfg.llm.api_key else 'NOT SET'})")
    print("\nServers:")
    for s in cfg.servers.values():
        print(f"  {s.name:<20} {s.host:<30} {','.join(s.tags)}")
    print("\nScripts:")
    for s in cfg.scripts.values():
        where = s.remote_path or f"local:{s.local_file}"
        print(f"  {s.name:<20} {where}  {s.description}")
    return 0


def cmd_run(cfg, args) -> int:
    scripts = [cfg.get_script(name) for name in args.script]
    servers = cfg.select_servers(args.servers, args.tags)
    redactor = Redactor(cfg.redact_patterns)

    if args.dry_run:
        runner = JumpServerRunner(cfg, executor=None)  # type: ignore[arg-type]
        if cfg.ccps.mode == "once":
            print(f"[jumpserver] {runner.wrap(cfg.ccps.command)}")
        for server in servers:
            for script in scripts:
                print(f"[{server.name}] {runner.build_command(server, script)}")
        return 0

    model = None if args.no_llm else OpenAIChatModel(cfg.llm)

    with ParamikoExecutor(cfg) as executor:
        runner = JumpServerRunner(cfg, executor)
        if cfg.ccps.mode == "once":
            runner.run_ccps()
        results = runner.run_many(servers, scripts)

    plan = None
    if model is not None:
        logging.info("Sending results to %s for analysis", cfg.llm.model)
        plan = analyze(results, model, cfg.llm, redactor)

    out_dir = write_report(cfg.reports_dir, plan, results, redactor)

    print()
    for r in results:
        print(f"  {r.server:<20} {r.script:<20} {r.status} (exit {r.exit_code})")
    if plan:
        print(f"\nOverall status: {plan.get('overall_status')}\n{plan.get('summary', '')}")
        print(f"\nAction plan: {out_dir / 'report.md'}")
    print(f"Raw output:  {out_dir / 'raw'}")
    return 0 if all(r.status == "ok" for r in results) else 2


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not args.verbose:
        logging.getLogger("paramiko").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        cfg = load_config(args.config)
        if args.command == "list":
            return cmd_list(cfg)
        return cmd_run(cfg, args)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        logging.debug("Fatal error", exc_info=True)
        print(f"Error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
