from __future__ import annotations

import argparse
import getpass
import logging
import sys

from . import __version__
from .app import LOOPBACK_NAMES, create_app
from .config import Settings, app_dir, load_dotenv_file
from .session import LoginError, SessionManager

log = logging.getLogger("oracle_jump_tool")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="oracle_jump_tool",
        description="Local Flask API that runs allowlisted Oracle scripts through a jump server.",
    )
    p.add_argument("command", nargs="?", default="serve", choices=["serve"])
    p.add_argument("--host", help="bind address (default 127.0.0.1 or JT_BIND_HOST)")
    p.add_argument("--port", type=int, help="bind port (default 5055 or JT_BIND_PORT)")
    p.add_argument("--jump-host", help="jump server hostname (default JT_JUMP_HOST)")
    p.add_argument("--no-prompt", action="store_true", help="skip the console login; use the web page or POST /login")
    p.add_argument("--debug", action="store_true", help="verbose logging")
    p.add_argument("--version", action="version", version=__version__)
    return p.parse_args(argv)


def console_login(manager: SessionManager, settings: Settings) -> None:
    print("\nLog in to the jump server (press Enter at Username to skip and use the web page).")
    for _ in range(3):
        host = input(f"Jump server [{settings.jump_host}]: ").strip() or settings.jump_host
        username = input("Username: ").strip()
        if not username:
            return
        password = getpass.getpass("Password (input hidden): ")
        print("Connecting, switching to oracle and running ccps ...")
        try:
            session = manager.login(username, password, host)
        except LoginError as exc:
            print(f"Login failed: {exc}")
            if exc.transcript:
                print("--- last output from the jump server ---")
                print(exc.transcript[-1500:])
                print("---")
            continue
        finally:
            password = ""
        print(f"Logged in as {session.username}@{session.jump_host}. Steps: {', '.join(session.shell.steps)}")
        return


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv_file()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    if args.host:
        settings.bind_host = args.host
    if args.port:
        settings.bind_port = args.port
    if args.jump_host:
        settings.jump_host = args.jump_host

    manager = SessionManager(settings)
    app = create_app(settings, manager)

    if settings.bind_host not in LOOPBACK_NAMES:
        log.warning("Listening on %s: other machines can reach this API. Use TLS and a firewall.",
                    settings.bind_host)

    if not args.no_prompt and sys.stdin and sys.stdin.isatty():
        try:
            console_login(manager, settings)
        except (EOFError, KeyboardInterrupt):
            print()

    base = f"http://{settings.bind_host}:{settings.bind_port}"
    print("\n" + "=" * 68)
    print(f" Oracle jump-server tool {__version__}  ->  {base}")
    print(f" Login page:  {base}/")
    if manager.api_key_generated:
        print(f" API key (new each start; set JT_API_KEY in {app_dir() / '.env'} to fix it):")
    else:
        print(" API key: from JT_API_KEY")
    print(f"   {manager.api_key if manager.api_key_generated else '(hidden)'}")
    print(" Agent header: Authorization: Bearer <API key>")
    print(" Press Ctrl+C to stop; the SSH session closes with it.")
    print("=" * 68 + "\n", flush=True)

    manager.start_reaper()
    try:
        try:
            from waitress import serve
        except ImportError:
            app.run(host=settings.bind_host, port=settings.bind_port, threaded=True)
        else:
            serve(app, host=settings.bind_host, port=settings.bind_port, threads=8,
                  channel_timeout=settings.max_command_timeout + 120, ident="oracle-jump-tool")
    except KeyboardInterrupt:
        pass
    finally:
        manager.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
