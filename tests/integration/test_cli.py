"""The `novelhog` command, run as real subprocesses against a temporary SQLite database."""

import os
import queue
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

TIMEOUT_S = 60


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    (tmp_path / "db").mkdir()
    return {
        **os.environ,
        "NOVELHOG_DATA_DIR": str(tmp_path),
        "NOVELHOG_DATABASE_URL": f"sqlite:///{tmp_path / 'db' / 'app.db'}",
        "NOVELHOG_SCRAPER_DIR": str(tmp_path / "scraper"),
    }


def _spawn(args: list[str], env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603 - test-only, fixed argv
        [sys.executable, "-m", "novelhog.cli", *args],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


@contextmanager
def _running(args: list[str], env: dict[str, str]) -> Iterator[subprocess.Popen[str]]:
    process = _spawn(args, env)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def _run(args: list[str], env: dict[str, str]) -> tuple[int, str]:
    with _running(args, env) as process:
        try:
            output, _ = process.communicate(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
            pytest.fail(f"{args} did not exit within {TIMEOUT_S}s; output:\n{output}")
        return process.returncode, output


def _wait_for(check: Callable[[], bool], seconds: float = 30) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.1)
    return False


def _serving(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
            return bool(response.status == 200)
    except urllib.error.URLError, OSError:
        return False


def _wait_for_output(process: subprocess.Popen[str], needle: str, seconds: float = 30) -> None:
    """Read the process's output until a line contains `needle`; kill and fail at the deadline."""
    stream = process.stdout
    assert stream is not None
    lines: queue.Queue[str] = queue.Queue()

    def pump() -> None:
        for line in stream:
            lines.put(line)

    threading.Thread(target=pump, daemon=True).start()
    seen: list[str] = []
    deadline = time.monotonic() + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            line = lines.get(timeout=min(remaining, 0.5))
        except queue.Empty:
            if process.poll() is not None and lines.empty():
                break
            continue
        seen.append(line)
        if needle in line:
            return
    process.kill()
    pytest.fail(f"no {needle!r} in output within {seconds}s; captured:\n{''.join(seen)}")


def _migrated(env: dict[str, str]) -> None:
    assert _run(["db", "migrate"], env)[0] == 0


class TestDbMigrate:
    def test_applies_pending_migrations_and_exits_0(self, env: dict[str, str]) -> None:
        code, _ = _run(["db", "migrate"], env)

        assert code == 0
        assert _run(["db", "status"], env)[0] == 0

    def test_exits_non_zero_with_the_alembic_error_when_a_migration_fails(
        self, env: dict[str, str]
    ) -> None:
        engine = create_engine(env["NOVELHOG_DATABASE_URL"])
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            connection.execute(text("INSERT INTO alembic_version VALUES ('nosuchrevision')"))
        engine.dispose()

        code, output = _run(["db", "migrate"], env)

        assert code != 0
        assert "nosuchrevision" in output


class TestDbStatus:
    def test_prints_revisions_and_exits_0_at_head(self, env: dict[str, str]) -> None:
        _migrated(env)

        code, output = _run(["db", "status"], env)

        assert code == 0
        assert "current: " in output
        assert "head: " in output
        assert "current: none" not in output

    def test_exits_1_when_not_at_head(self, env: dict[str, str]) -> None:
        code, output = _run(["db", "status"], env)

        assert code == 1
        assert "current: none" in output


class TestApi:
    def test_migrate_flag_migrates_then_serves(self, env: dict[str, str]) -> None:
        port = _free_port()
        url = f"http://127.0.0.1:{port}/api/v1/settings"

        with _running(["api", "--migrate", "--port", str(port)], env) as process:
            assert _wait_for(lambda: _serving(url))
            process.send_signal(signal.SIGTERM)
            # uvicorn re-raises the signal it handled, so the status is 0 or -SIGTERM.
            assert process.wait(timeout=TIMEOUT_S) in (0, -signal.SIGTERM)

    @pytest.mark.parametrize("command", [["api"], ["web"], ["worker"]])
    def test_outdated_database_exits_non_zero_with_migrate_hint(
        self, env: dict[str, str], command: list[str]
    ) -> None:
        code, output = _run(command, env)

        assert code != 0
        assert "run `novelhog db migrate`" in output


class TestWorker:
    def test_exits_0_within_2s_of_sigterm_while_idle(self, env: dict[str, str]) -> None:
        _migrated(env)
        with _running(["worker"], env) as process:
            _wait_for_output(process, "worker_started")
            process.send_signal(signal.SIGTERM)
            start = time.monotonic()
            code = process.wait(timeout=TIMEOUT_S)

        assert code == 0
        assert time.monotonic() - start < 2


def _fake_caddy(directory: Path, body: str) -> dict[str, str]:
    directory.mkdir()
    script = directory / "caddy"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return {"PATH": f"{directory}{os.pathsep}{os.environ['PATH']}"}


class TestWeb:
    def test_exits_non_zero_when_caddy_exits(self, env: dict[str, str], tmp_path: Path) -> None:
        _migrated(env)
        env |= _fake_caddy(tmp_path / "bin", "sleep 1; exit 3")

        code, _ = _run(["web", "--api-port", str(_free_port())], env)

        assert code == 3

    def test_exits_non_zero_when_the_api_exits(self, env: dict[str, str], tmp_path: Path) -> None:
        _migrated(env)
        marker = tmp_path / "caddy-started"
        env |= _fake_caddy(tmp_path / "bin", f"touch {marker}; exec sleep 60")
        # A port that is already taken makes the API child exit at startup.
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]
            code, _ = _run(["web", "--api-port", str(port)], env)

        assert code != 0
        assert marker.exists()

    def test_starts_both_and_stops_both_on_sigterm(
        self, env: dict[str, str], tmp_path: Path
    ) -> None:
        _migrated(env)
        marker = tmp_path / "caddy-started"
        pid_file = tmp_path / "caddy.pid"
        env |= _fake_caddy(tmp_path / "bin", f"touch {marker}; echo $$ > {pid_file}; exec sleep 60")
        port = _free_port()
        url = f"http://127.0.0.1:{port}/api/v1/settings"

        with _running(["web", "--api-port", str(port)], env) as process:
            assert _wait_for(lambda: _serving(url))
            assert _wait_for(marker.exists)
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=TIMEOUT_S) == 0

        caddy_pid = int(pid_file.read_text())
        assert not _wait_for(lambda: _alive(caddy_pid), seconds=0.5)
        assert not _serving(url)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
