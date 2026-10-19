"""The scraper's own logging and import-time warnings stay out of the worker's output."""

import subprocess
import sys
from pathlib import Path

from structlog.testing import capture_logs

import novelhog.scraper_adapter  # noqa: F401  (takes over the scraper's logger)
from novelhog.scraper_adapter.scraper import ScraperError

_SCRAPER_LOGGER = "web_novel_scraper.logger_manager"


def scraper_logger(operation: str):  # type: ignore[no-untyped-def]  # noqa: ANN202
    from web_novel_scraper.logger_manager import create_logger

    return create_logger(operation)


def run_python(code: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {"PYTHONPYCACHEPREFIX": str(tmp_path / "pyc"), "PATH": ""}
    return subprocess.run(
        [sys.executable, "-W", "error::SyntaxWarning", "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )


class TestScraperLogging__OnInfo:
    def test_scraper_log__info_reaches_neither_stderr_nor_stdout(self, tmp_path: Path) -> None:
        code = (
            "import novelhog.scraper_adapter\n"
            "from web_novel_scraper.logger_manager import create_logger\n"
            "create_logger('t').info('hello from the scraper')\n"
        )
        done = run_python(code, tmp_path)
        assert (done.returncode, done.stdout, done.stderr) == (0, "", "")

    def test_scraper_log__info_is_not_a_structlog_event(self) -> None:
        with capture_logs() as events:
            scraper_logger("t").info("quiet")
        assert events == []


class TestScraperLogging__OnWarning:
    def test_scraper_log__warning_arrives_as_structlog_event(self) -> None:
        with capture_logs() as events:
            scraper_logger("op").warning("careful %s", "now")
        assert events == [
            {
                "event": "scraper_log",
                "log_level": "warning",
                "message": "careful now",
                "scraper_module": "op",
            }
        ]

    def test_scraper_log__error_includes_exception_summary_without_ansi(self) -> None:
        with capture_logs() as events:
            scraper_logger("op").error("failed", exc_info=ScraperError("boom"))
        assert events[0]["log_level"] == "error"
        assert events[0]["message"] == "failed: ScraperError: boom"
        assert "\x1b" not in events[0]["message"]


class TestScraperImport__OnSyntaxWarning:
    def test_import__does_not_raise_the_scrapers_syntax_warning(self, tmp_path: Path) -> None:
        done = run_python("import novelhog.scraper_adapter", tmp_path)
        assert (done.returncode, done.stderr) == (0, "")
