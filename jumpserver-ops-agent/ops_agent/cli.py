from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

from .config import ConfigError, load_config
from .runner import execute_run


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

    web = sub.add_parser("web", help="Open the browser UI (Flask) on this PC")
    web.add_argument("--host", default="127.0.0.1", help="Bind address (default: this PC only)")
    web.add_argument("--port", type=int, default=5000)
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
    outcome = execute_run(cfg, args.script, args.servers, args.tags,
                          dry_run=args.dry_run, no_llm=args.no_llm)
    if outcome.dry_run:
        print("\n".join(outcome.commands))
        return 0
    print()
    for result in outcome.results:
        print(f"  {result.server:<20} {result.script:<20} {result.status} (exit {result.exit_code})")
    if outcome.plan:
        print(f"\nOverall status: {outcome.plan.get('overall_status')}\n{outcome.plan.get('summary', '')}")
        print(f"\nAction plan: {outcome.report_dir / 'report.md'}")
    print(f"Raw output:  {outcome.report_dir / 'raw'}")
    return outcome.exit_code


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
        if args.command == "web":
            from .web import serve
            serve(args.config, args.host, args.port)
            return 0
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
