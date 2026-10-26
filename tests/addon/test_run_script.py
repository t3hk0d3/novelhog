"""The Home Assistant add-on's startup script (`homeassistant/novelhog/run.sh`), run as it ships.

Each test runs the real script in a subprocess with `OPTIONS_FILE` pointing at a temporary options
file and a directory of stub executables first on `PATH`. The stub `novelhog` records what it was
started with and then exits, or waits for SIGTERM, as the test directs. No Docker and no
application code are involved; `jq` (which the add-on image installs) must be on the host.
"""

import os
import shutil
import signal
import subprocess  # noqa: S404 - the tests run the script under test
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="the add-on script needs bash and jq",
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "homeassistant" / "novelhog" / "run.sh"
TIMEOUT_S = 10.0

STUB = """#!/usr/bin/env bash
# Stand-in for `novelhog`: records its start and environment, then exits or waits for SIGTERM.
role="$1"
if [[ "$1" == db ]]; then role=migrate; fi
echo "START $role" >> "$STUB_DIR/log"
env > "$STUB_DIR/env.$role"
if [[ "$role" == migrate ]]; then exit "${STUB_MIGRATE_STATUS:-0}"; fi
mode_var="STUB_${role^^}"
mode="${!mode_var:-run}"
trap 'echo "TERM $role" >> "$STUB_DIR/log"; exit 0' TERM
if [[ "$mode" == exit:* ]]; then
    sleep 0.3
    echo "EXIT $role" >> "$STUB_DIR/log"
    exit "${mode#exit:}"
fi
while true; do
    sleep 0.1
done
"""

EVERY_OPTION = """{
  "public_url": "https://novelhog.example.com",
  "vapid_subject": "mailto:you@example.com",
  "allowed_hosts": ["homeserver", "novelhog.local"],
  "flaresolverr_url": "http://flaresolverr:8191",
  "database_url": "postgresql+psycopg://novelhog:secret@db:5432/novelhog"
}"""
EMPTY_OPTIONS = """{
  "public_url": "",
  "vapid_subject": null,
  "allowed_hosts": [],
  "flaresolverr_url": "",
  "database_url": ""
}"""
OPTION_VARIABLES = [
    "NOVELHOG_PUBLIC_URL",
    "NOVELHOG_VAPID_SUBJECT",
    "NOVELHOG_ALLOWED_HOSTS",
    "NOVELHOG_FLARESOLVERR_URL",
    "NOVELHOG_DATABASE_URL",
]


@dataclass
class Run:
    """What one run of the script left behind."""

    returncode: int
    stderr: str
    stub_dir: Path
    log: list[str] = field(default_factory=list)

    def environment(self, role: str) -> dict[str, str]:
        """The environment `novelhog <role>` was started with."""
        lines = (self.stub_dir / f"env.{role}").read_text().splitlines()
        return dict(line.split("=", 1) for line in lines if "=" in line)

    def started(self, role: str) -> bool:
        return f"START {role}" in self.log


def _read_log(stub_dir: Path) -> list[str]:
    log = stub_dir / "log"
    return log.read_text().splitlines() if log.exists() else []


def _wait_for(process: subprocess.Popen[str], stub_dir: Path, line: str) -> None:
    """Wait until the stub logs `line`; fail at once if the script has already ended."""
    deadline = time.monotonic() + TIMEOUT_S
    while line not in _read_log(stub_dir):
        if process.poll() is not None:
            msg = f"the script ended (status {process.returncode}) before the stub logged {line!r}"
            raise AssertionError(msg)
        if time.monotonic() > deadline:
            msg = f"the stub never logged {line!r}"
            raise TimeoutError(msg)
        time.sleep(0.05)


def run_script(
    tmp_path: Path,
    options: str | None,
    *,
    migrate_status: int = 0,
    web: str = "run",
    worker: str = "run",
    terminate: bool = True,
) -> Run:
    """Run the script until it ends; `terminate` sends SIGTERM once web and worker are up."""
    # Without this, "starts nothing" and "exits non-zero" would pass for a script that is not there.
    assert SCRIPT.is_file(), f"{SCRIPT} does not exist"
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    stub = stub_dir / "novelhog"
    stub.write_text(STUB)
    stub.chmod(0o755)
    options_file = tmp_path / "options.json"
    if options is not None:
        options_file.write_text(options)
    env = {k: v for k, v in os.environ.items() if not k.startswith("NOVELHOG_")}
    env.update(
        PATH=f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        OPTIONS_FILE=str(options_file),
        STUB_DIR=str(stub_dir),
        STUB_MIGRATE_STATUS=str(migrate_status),
        STUB_WEB=web,
        STUB_WORKER=worker,
    )
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        ["bash", str(SCRIPT)],  # noqa: S607
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        if terminate:
            _wait_for(process, stub_dir, "START web")
            _wait_for(process, stub_dir, "START worker")
            process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=TIMEOUT_S)
    except BaseException:
        process.kill()
        raise
    return Run(process.returncode, stderr, stub_dir, _read_log(stub_dir))


class TestRunScript__WithEveryOptionSet:
    @pytest.mark.parametrize("role", ["web", "worker"])
    def test_run__web_and_worker_see_every_option(self, tmp_path: Path, role: str) -> None:
        env = run_script(tmp_path, EVERY_OPTION).environment(role)
        assert [env[name] for name in OPTION_VARIABLES] == [
            "https://novelhog.example.com",
            "mailto:you@example.com",
            "homeserver,novelhog.local",
            "http://flaresolverr:8191",
            "postgresql+psycopg://novelhog:secret@db:5432/novelhog",
        ]

    def test_run__migrate_sees_the_database_url(self, tmp_path: Path) -> None:
        env = run_script(tmp_path, EVERY_OPTION).environment("migrate")
        assert (
            env["NOVELHOG_DATABASE_URL"] == "postgresql+psycopg://novelhog:secret@db:5432/novelhog"
        )

    @pytest.mark.parametrize("role", ["migrate", "web", "worker"])
    def test_run__every_process_sees_the_data_dir(self, tmp_path: Path, role: str) -> None:
        assert run_script(tmp_path, EVERY_OPTION).environment(role)["NOVELHOG_DATA_DIR"] == "/data"


class TestRunScript__WithEmptyOptions:
    @pytest.mark.parametrize("name", OPTION_VARIABLES)
    def test_run__the_variable_is_not_set(self, tmp_path: Path, name: str) -> None:
        run = run_script(tmp_path, EMPTY_OPTIONS)
        assert all(name not in run.environment(role) for role in ["migrate", "web", "worker"])


class TestRunScript__WithNoOptionsFile:
    def test_run__starts_with_defaults(self, tmp_path: Path) -> None:
        run = run_script(tmp_path, None)
        assert run.started("web")
        assert run.started("worker")
        assert "NOVELHOG_DATABASE_URL" not in run.environment("web")


class TestRunScript__WithAMalformedOptionsFile:
    def test_run__exits_non_zero_naming_the_file(self, tmp_path: Path) -> None:
        run = run_script(tmp_path, "{not json", terminate=False)
        assert run.returncode != 0
        assert "options.json" in run.stderr

    def test_run__starts_no_novelhog_process(self, tmp_path: Path) -> None:
        assert run_script(tmp_path, "{not json", terminate=False).log == []


class TestRunScript__Starting:
    def test_run__migrates_before_web_and_worker_start(self, tmp_path: Path) -> None:
        log = run_script(tmp_path, EVERY_OPTION).log
        assert log.index("START migrate") < log.index("START web")
        assert log.index("START migrate") < log.index("START worker")

    def test_run__starts_web_and_worker(self, tmp_path: Path) -> None:
        run = run_script(tmp_path, EVERY_OPTION)
        assert run.started("web")
        assert run.started("worker")


class TestRunScript__WhenTheMigrationFails:
    def test_run__exits_with_the_migrations_status(self, tmp_path: Path) -> None:
        run = run_script(tmp_path, EVERY_OPTION, migrate_status=7, terminate=False)
        assert run.returncode == 7

    def test_run__starts_neither_web_nor_worker(self, tmp_path: Path) -> None:
        run = run_script(tmp_path, EVERY_OPTION, migrate_status=7, terminate=False)
        assert run.log == ["START migrate"]


class TestRunScript__OnSigterm:
    @pytest.mark.parametrize("role", ["web", "worker"])
    def test_run__both_children_receive_sigterm(self, tmp_path: Path, role: str) -> None:
        assert f"TERM {role}" in run_script(tmp_path, EVERY_OPTION).log

    def test_run__waits_for_them_and_exits_zero(self, tmp_path: Path) -> None:
        run = run_script(tmp_path, EVERY_OPTION)
        assert run.returncode == 0
        # The children log their SIGTERM before they exit, so a finished script has waited for them.
        assert {"TERM web", "TERM worker"} <= set(run.log)


class TestRunScript__WhenAChildExitsOnItsOwn:
    def test_run__the_other_child_receives_sigterm(self, tmp_path: Path) -> None:
        run = run_script(tmp_path, EVERY_OPTION, web="exit:3", terminate=False)
        assert "TERM worker" in run.log

    def test_run__exits_non_zero(self, tmp_path: Path) -> None:
        run = run_script(tmp_path, EVERY_OPTION, web="exit:3", terminate=False)
        assert run.returncode != 0

    def test_run__exits_non_zero_even_when_the_child_exited_cleanly(self, tmp_path: Path) -> None:
        run = run_script(tmp_path, EVERY_OPTION, worker="exit:0", terminate=False)
        assert run.returncode != 0
