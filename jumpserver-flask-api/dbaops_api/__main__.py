import argparse
import logging
import os
import sys

from .config import ConfigError, load_config


DEFAULT_ENV_FILE = "/etc/dbaops-api/dbaops.env"


def load_env_file(path):
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


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3.6 -m dbaops_api")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE,
                        help=f"KEY=VALUE file to load first (default {DEFAULT_ENV_FILE})")
    parser.add_argument("-c", "--config", default=None)
    sub = parser.add_subparsers(dest="cmd")

    serve = sub.add_parser("serve", help="run the API with Flask's threaded server (testing)")
    serve.add_argument("--host", default=os.environ.get("DBAOPS_BIND", "0.0.0.0"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("DBAOPS_PORT", "8080")))

    sub.add_parser("check", help="validate config.json")

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.error("a command is required")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_env_file(args.env_file)
    args.config = args.config or os.environ.get("DBAOPS_CONFIG", "config.json")

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    if args.cmd == "check":
        print(f"Config OK: {args.config}")
        print(f"Scripts: {', '.join(cfg.scripts)}")
        print(f"Actual jump host: {cfg.actual_jump_host}:8080")
        print(f"Target known hosts: {cfg.target_known_hosts}")
        print(f"Allowed target hosts: {len(cfg.target_allowed_hosts)}")
        print(f"Allowed target CIDRs: {len(cfg.target_allowed_cidrs)}")
        print(f"Allowed target ports: {cfg.target_allowed_ports}")
        print(f"API token ({cfg.api_token_env}): {'set' if cfg.api_token else 'NOT SET'}")
        print(f"ccps secret ({cfg.ccps_secret_env}): {'set' if cfg.ccps_secret else 'NOT SET'}")
        cfg.validate_secrets()
        return 0

    if args.cmd == "serve":
        from .app import create_app
        app = create_app(args.config)
        if args.host not in {"127.0.0.1", "localhost"}:
            logging.warning(
                "Listening on %s:%s over HTTP. BIG-IP must restrict sources; "
                "prefer a TLS client-side profile.", args.host, args.port)
        app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
