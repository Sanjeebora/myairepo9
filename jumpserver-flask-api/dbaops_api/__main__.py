from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys

from .auth import SessionManager
from .config import ConfigError, load_config
from .runner import Runner


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3.8 -m dbaops_api")
    parser.add_argument("-c", "--config", default=os.environ.get("DBAOPS_CONFIG", "config.json"))
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run the API with Flask's threaded server")
    serve.add_argument("--host", default=os.environ.get("DBAOPS_BIND", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("DBAOPS_PORT", "8080")))

    tok = sub.add_parser("token", help="print a session token for the agent harness")
    tok.add_argument("--client", default="agent")
    tok.add_argument("--json", action="store_true")

    sub.add_parser("check", help="validate config.json and the environment")
    sub.add_parser("gen-secret", help="print a random value for DBAOPS_SECRET_KEY / DBAOPS_API_KEY")

    dry = sub.add_parser("dry-run", help="print the exact command /run would execute")
    dry.add_argument("--host", required=True)
    dry.add_argument("--script", required=True)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.cmd == "gen-secret":
        print(secrets.token_urlsafe(48))
        return 0

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    if args.cmd == "check":
        print(f"Config OK: {args.config}")
        print(f"Scripts: {', '.join(cfg.scripts)}")
        for var in ("DBAOPS_SECRET_KEY", "DBAOPS_API_KEY"):
            print(f"{var}: {'set' if os.environ.get(var) else 'NOT SET'}")
        if cfg.llm.enabled:
            print(f"LLM: {cfg.llm.model} at {cfg.llm.base_url} "
                  f"(key {'set' if cfg.llm.api_key else 'NOT SET'})")
        return 0

    if args.cmd == "dry-run":
        try:
            host = cfg.validate_host(args.host)
            script = cfg.get_script(args.script)
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return 1
        runner = Runner(cfg)
        print("ssh command :", runner.ssh_command(host, script))
        print("argv        :", json.dumps(runner.argv(host, script)))
        print("oracle shell:\n" + runner.oracle_shell(host, script))
        return 0

    try:
        sessions = SessionManager.from_env(cfg.session_ttl_seconds)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.cmd == "token":
        token, expires_at = sessions.issue(args.client)
        if args.json:
            print(json.dumps({"token": token, "expires_at": expires_at, "expires_in": sessions.ttl}))
        else:
            print(token)
        return 0

    from .app import create_app
    app = create_app(args.config, sessions=sessions)
    if args.host not in {"127.0.0.1", "localhost"}:
        logging.warning("Listening on %s:%s. Restrict access with firewalld to the agent host.",
                        args.host, args.port)
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
