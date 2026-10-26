"""The `novelhog` console command: db migrate/status, api, web and worker (spec sections 2, 4)."""

import argparse
import os
import signal
import subprocess  # noqa: S404 - `web` supervises uvicorn and Caddy as child processes
import sys
import time
from collections.abc import Sequence
from types import FrameType
from typing import Literal

from fastapi import FastAPI

from novelhog import log, net
from novelhog.api import serve
from novelhog.api.app import create_app
from novelhog.config import Settings, load
from novelhog.flaresolverr import FlareSolverr
from novelhog.image_fetcher import ImageFetcher
from novelhog.notifications.channels import RegistryHolder
from novelhog.repo import Repo, SchemaOutdated
from novelhog.scraper_adapter import ScraperAdapter
from novelhog.storage import LocalStaticStorage
from novelhog.worker.loop import Worker

API_HOST = "127.0.0.1"
API_PORT = 8000
GRACEFUL_SHUTDOWN_S = 3  # open SSE streams must not hold up a stop
CHILD_STOP_S = 10.0  # `web` kills a child that ignores SIGTERM after this long
POLL_S = 0.1
# Task 30 owns docker/Caddyfile; the image decides where it lands and tells us through this.
CADDYFILE_ENV = "NOVELHOG_CADDYFILE"
DEFAULT_CADDYFILE = "/etc/caddy/Caddyfile"
EXIT_FAILURE = 1
EXIT_USAGE = 2


def _repo(settings: Settings) -> Repo:
    return Repo(
        settings.database_url,
        static_base_url=settings.static_base_url,
        refresh_min_interval=settings.refresh_min_interval,
        refresh_max_interval=settings.refresh_max_interval,
    )


def _report_outdated(error: SchemaOutdated) -> int:
    log.get().error("schema_outdated", current=error.current or "none", head=error.head)
    print(error, file=sys.stderr)
    return EXIT_FAILURE


def _require_current(settings: Settings) -> None:
    repo = _repo(settings)
    try:
        repo.require_current()
    finally:
        repo.engine.dispose()


def _db_migrate(settings: Settings) -> int:
    repo = _repo(settings)
    try:
        head = repo.migrate()
    except Exception as error:  # noqa: BLE001 - surface any Alembic or driver failure and exit 1
        log.get().exception("migration_failed")
        print(f"migration failed: {type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_FAILURE
    finally:
        repo.engine.dispose()
    print(f"database is at {head}")
    return 0


def _db_status(settings: Settings) -> int:
    repo = _repo(settings)
    try:
        state = repo.revisions()
    finally:
        repo.engine.dispose()
    print(f"current: {state.current or 'none'}")
    print(f"head: {state.head}")
    return 0 if state.is_current else EXIT_FAILURE


def _serve(app: FastAPI, host: str, port: int) -> int:
    """Run `app` under uvicorn until it is stopped. The only place uvicorn is started."""
    serve.run(app, host=host, port=port, graceful_s=GRACEFUL_SHUTDOWN_S)
    return 0


def _api(settings: Settings, *, migrate: bool, port: int) -> int:
    if migrate:
        status = _db_migrate(settings)
        if status != 0:
            return status
    try:
        _require_current(settings)
    except SchemaOutdated as error:
        return _report_outdated(error)
    return _serve(create_app(settings), API_HOST, port)


def _worker(settings: Settings) -> int:
    repo = _repo(settings)
    session = net.GuardedSession(allow_private=settings.allow_private_fetch)
    # One FlareSolverr session and clearance cache per worker, for pages and images alike.
    flaresolverr = FlareSolverr(settings.flaresolverr_url, settings.worker_id)
    worker = Worker(
        repo,
        ScraperAdapter(settings, flaresolverr=flaresolverr),
        ImageFetcher(
            session,
            max_bytes=settings.max_image_bytes,
            max_pixels=settings.max_image_pixels,
            max_dimension=settings.image_max_dimension,
            flaresolverr=flaresolverr,
        ),
        LocalStaticStorage(settings.static_dir, settings.static_base_url),
        RegistryHolder(settings, repo, session).get,
        settings,
    )

    def stop(_signum: int, _frame: FrameType | None) -> None:
        worker.request_stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        worker.run()
    except SchemaOutdated as error:
        return _report_outdated(error)
    finally:
        flaresolverr.close()
        repo.engine.dispose()
    return 0


def _terminate(children: Sequence[subprocess.Popen[bytes]]) -> None:
    """SIGTERM every live child, then SIGKILL the ones still alive after `CHILD_STOP_S`."""
    for child in children:
        if child.poll() is None:
            child.terminate()
    deadline = time.monotonic() + CHILD_STOP_S
    for child in children:
        try:
            child.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def _web(settings: Settings, *, api_port: int) -> int:
    try:
        _require_current(settings)
    except SchemaOutdated as error:
        return _report_outdated(error)
    caddyfile = os.environ.get(CADDYFILE_ENV) or DEFAULT_CADDYFILE
    stopping: list[int] = []

    def forward(signum: int, _frame: FrameType | None) -> None:
        stopping.append(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    children: list[subprocess.Popen[bytes]] = []
    exited: list[int] = []  # exit codes of children that stopped on their own
    try:
        api_command = [sys.executable, "-m", "novelhog.cli", "api", "--port", str(api_port)]
        children.append(subprocess.Popen(api_command))  # noqa: S603 - fixed argv, no shell
        caddy_command = ["caddy", "run", "--config", caddyfile, "--adapter", "caddyfile"]
        children.append(subprocess.Popen(caddy_command))  # noqa: S603, S607 - Caddy from PATH
        while not stopping and all(child.poll() is None for child in children):
            time.sleep(POLL_S)
        exited = [code for child in children if (code := child.poll()) is not None]
    except OSError as error:
        print(f"cannot start child process: {error}", file=sys.stderr)
        return EXIT_FAILURE
    finally:
        _terminate(children)
    if stopping:
        return 0
    log.get().error("child_exited", codes=exited)
    return next((code for code in exited if code > 0), EXIT_FAILURE)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novelhog")
    commands = parser.add_subparsers(dest="command", required=True)
    db = commands.add_parser("db", help="database commands")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    db_commands.add_parser("migrate", help="apply pending migrations")
    db_commands.add_parser("status", help="print current and head revisions; exit 1 if outdated")
    api = commands.add_parser("api", help="serve the API on 127.0.0.1:8000")
    api.add_argument("--migrate", action="store_true", help="migrate first (development only)")
    api.add_argument("--port", type=int, default=API_PORT, help=argparse.SUPPRESS)
    web = commands.add_parser("web", help="serve the API and Caddy")
    # Test-only seam: Caddy's upstream stays on 8000, so this is not a deployment setting.
    web.add_argument("--api-port", type=int, default=API_PORT, help=argparse.SUPPRESS)
    commands.add_parser("worker", help="run the worker")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command in `argv` (default `sys.argv[1:]`) and return its exit status."""
    args = _parser().parse_args(argv)
    service: Literal["api", "worker", "cli"]
    service = {"db": "cli", "worker": "worker"}.get(args.command, "api")  # type: ignore[assignment]
    log.setup(service)
    try:
        settings = load()
    except ValueError as error:
        print(error, file=sys.stderr)
        return EXIT_USAGE
    match args.command:
        case "db" if args.db_command == "migrate":
            return _db_migrate(settings)
        case "db":
            return _db_status(settings)
        case "api":
            return _api(settings, migrate=args.migrate, port=args.port)
        case "web":
            return _web(settings, api_port=args.api_port)
        case _:
            return _worker(settings)


if __name__ == "__main__":
    sys.exit(main())
