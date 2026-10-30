from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys

from .config import ConfigError, load_config
from .runner import Runner


DEFAULT_ENV_FILE = "/etc/dbaops-api/dbaops.env"


def load_env_file(path: str) -> None:
    """Read KEY=VALUE lines (the systemd EnvironmentFile) without overriding the environment."""
    try:
        lines = open(path).read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3.8 -m dbaops_api")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE,
                        help=f"KEY=VALUE file to load first (default {DEFAULT_ENV_FILE})")
    parser.add_argument("-c", "--config", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run the API with Flask's threaded server (testing)")
    serve.add_argument("--host", default=os.environ.get("DBAOPS_BIND", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("DBAOPS_PORT", "8080")))

    sub.add_parser("check", help="validate config.json")

    dry = sub.add_parser("dry-run", help="print the exact command /run would execute")
    dry.add_argument("--host", required=True)
    dry.add_argument("--script", required=True)

    try_ = sub.add_parser("try", help="log in as yourself and run one script, without HTTP")
    try_.add_argument("--user", default=getpass.getuser())
    try_.add_argument("--host", required=True)
    try_.add_argument("--script", required=True)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_env_file(args.env_file)
    args.config = args.config or os.environ.get("DBAOPS_CONFIG", "config.json")

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    if args.cmd == "check":
        j = cfg.jumpserver
        print(f"Config OK: {args.config}")
        print(f"Scripts: {', '.join(cfg.scripts)}")
        print(f"People log in to {j.host}:{j.port}; host keys from {j.known_hosts}")
        print(f"ccps passphrase ({cfg.ccps_passphrase_env}): "
              f"{'set' if cfg.ccps_passphrase else 'NOT SET (only fine if cp993 has no passphrase)'}")
        if cfg.llm.enabled:
            print(f"LLM: {cfg.llm.model} at {cfg.llm.base_url} "
                  f"(key {'set' if cfg.llm.api_key else 'NOT SET'})")
        return 0

    if args.cmd == "serve":
        from .app import create_app
        app = create_app(args.config)
        if args.host not in {"127.0.0.1", "localhost"}:
            logging.warning("Listening on %s:%s over plain HTTP. Use gunicorn with TLS for real use.",
                            args.host, args.port)
        app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
        return 0

    runner = Runner(cfg)
    try:
        host = cfg.validate_host(args.host)
        script = cfg.get_script(args.script)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.cmd == "dry-run":
        print("ssh command :", runner.ssh_command(host, script))
        print("jump command:", runner.command(host, script))
        print("oracle shell:\n" + runner.oracle_shell(host, script))
        return 0

    from .jumphost import JumpSession
    from .sessions import JumpUnavailable, LoginFailed
    password = getpass.getpass(f"{args.user}@{cfg.jumpserver.host} password: ")
    try:
        session = JumpSession(cfg.jumpserver, args.user, password)
    except (LoginFailed, JumpUnavailable) as exc:
        print(exc, file=sys.stderr)
        return 1
    try:
        result = runner.run(session, host, script)
    finally:
        session.close()
    print(json.dumps({"status": result.status, "exit_code": result.exit_code,
                      "command": result.command, "error": result.error,
                      "stderr": result.stderr}, indent=2))
    print("----- output -----")
    print(result.output)
    return 0 if result.status == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
