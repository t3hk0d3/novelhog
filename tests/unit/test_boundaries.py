"""Project boundaries: each import-linter contract fails on a throwaway offending module."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_lint_imports(project: Path) -> subprocess.CompletedProcess[str]:
    lint_imports = Path(sys.executable).with_name("lint-imports")
    return subprocess.run(  # noqa: S603
        [str(lint_imports)],
        cwd=project,
        env={"PATH": str(Path(sys.executable).parent), "PYTHONPATH": str(project / "src")},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A temporary copy of the package and its lint configuration."""
    shutil.copy(REPO_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    shutil.copytree(
        REPO_ROOT / "src",
        tmp_path / "src",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        REPO_ROOT / "tools",
        tmp_path / "tools",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    return tmp_path


def _add_module(project: Path, dotted: str, source: str) -> None:
    path = project / "src" / Path(*dotted.split("."))
    path = path.with_suffix(".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)


class TestUnchangedTree:
    def test_lint_imports__passes_on_real_tree(self) -> None:
        result = _run_lint_imports(REPO_ROOT)
        assert result.returncode == 0, result.stdout + result.stderr


class TestApiIsPure__WhenApiImportsWorkerOnlyModule:
    @pytest.mark.parametrize(
        "forbidden",
        [
            "novelhog.scraper_adapter",
            "novelhog.exporter",
            "novelhog.images",
            "novelhog.image_fetcher",
            "novelhog.html_to_markdown",
            "novelhog.notifications.channels",
            "novelhog.net",
            "novelhog.notifications.dispatcher",
            "novelhog.worker",
            "PIL",
            "pywebpush",
            "web_novel_scraper",
        ],
    )
    def test_lint_imports__fails_naming_the_forbidden_import(
        self, project: Path, forbidden: str
    ) -> None:
        if forbidden == "novelhog.notifications.dispatcher":
            _add_module(project, forbidden, '"""dispatcher."""\n')
        _add_module(project, "novelhog.api.offender", f"import {forbidden}\n")
        result = _run_lint_imports(project)
        assert result.returncode != 0
        assert "api-is-pure BROKEN" in result.stdout
        assert f"novelhog.api.offender -> {forbidden}" in result.stdout

    def test_lint_imports__passes_when_api_imports_repo(self, project: Path) -> None:
        _add_module(project, "novelhog.api.fine", "import novelhog.repo\n")
        assert _run_lint_imports(project).returncode == 0


class TestRepoIsStandalone__WhenRepoImportsOutsideItsAllowance:
    @pytest.mark.parametrize(
        "forbidden",
        [
            "novelhog.config",
            "novelhog.storage",
            "novelhog.worker",
            "novelhog.log",
            "fastapi",
            "PIL",
            "pydantic",
            "psycopg",
        ],
    )
    def test_lint_imports__fails(self, project: Path, forbidden: str) -> None:
        if forbidden == "novelhog.log":
            _add_module(project, "novelhog.log", '"""log."""\n')
        _add_module(project, "novelhog.repo.offender", f"import {forbidden}\n")
        result = _run_lint_imports(project)
        assert result.returncode != 0
        assert "repo-is-standalone BROKEN" in result.stdout
        assert f"novelhog.repo.offender -> {forbidden}" in result.stdout

    @pytest.mark.parametrize(
        "allowed",
        ["sqlalchemy", "alembic", "novelhog.ids", "novelhog.domain", "json"],
    )
    def test_lint_imports__passes_for_allowed_imports(self, project: Path, allowed: str) -> None:
        _add_module(project, "novelhog.ids", '"""ids."""\n')
        _add_module(project, "novelhog.domain", '"""domain."""\n')
        _add_module(project, "novelhog.repo.fine", f"import {allowed}\n")
        assert _run_lint_imports(project).returncode == 0


class TestScraperIsIsolated__WhenAnotherModuleImportsWebNovelScraper:
    @pytest.mark.parametrize("module", ["novelhog.worker.offender", "novelhog.repo.offender"])
    def test_lint_imports__fails(self, project: Path, module: str) -> None:
        _add_module(project, module, "import web_novel_scraper\n")
        result = _run_lint_imports(project)
        assert result.returncode != 0
        assert "scraper-is-isolated BROKEN" in result.stdout
        assert f"{module} -> web_novel_scraper" in result.stdout

    def test_lint_imports__passes_for_scraper_adapter(self, project: Path) -> None:
        _add_module(project, "novelhog.scraper_adapter.fine", "import web_novel_scraper\n")
        assert _run_lint_imports(project).returncode == 0

    def test_lint_imports__passes_when_worker_uses_scraper_adapter(self, project: Path) -> None:
        _add_module(project, "novelhog.scraper_adapter.fine", "import web_novel_scraper\n")
        _add_module(project, "novelhog.worker.fine", "import novelhog.scraper_adapter.fine\n")
        assert _run_lint_imports(project).returncode == 0


def _run_check_sh(
    tmp_path: Path, fail_on: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run check.sh with a fake `uv` that logs its arguments and fails on `fail_on`."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "uv.log"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        f'#!/usr/bin/env bash\necho "$*" >> "{log}"\n[ "$*" = "{fail_on}" ] && exit 1\nexit 0\n'
    )
    fake_uv.chmod(0o755)
    result = subprocess.run(  # noqa: S603
        ["bash", str(REPO_ROOT / "scripts" / "check.sh")],  # noqa: S607
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


class TestCheckScript__OnCleanTree:
    def test_check_sh__runs_every_gate_in_order(self, tmp_path: Path) -> None:
        _, calls = _run_check_sh(tmp_path, fail_on="never")
        assert calls == [
            "run ruff format --check",
            "run ruff check",
            "run mypy --strict src tests tools",
            "run lint-imports",
            "run pytest",
        ]

    def test_check_sh__exits_zero(self, tmp_path: Path) -> None:
        result, _ = _run_check_sh(tmp_path, fail_on="never")
        assert result.returncode == 0


class TestCheckScript__OnFailingGate:
    def test_check_sh__exits_non_zero_and_names_the_gate(self, tmp_path: Path) -> None:
        result, _ = _run_check_sh(tmp_path, fail_on="run mypy --strict src tests tools")
        assert result.returncode != 0
        assert "FAILED gate: mypy --strict" in result.stderr

    def test_check_sh__does_not_run_later_gates(self, tmp_path: Path) -> None:
        _, calls = _run_check_sh(tmp_path, fail_on="run mypy --strict src tests tools")
        assert calls == [
            "run ruff format --check",
            "run ruff check",
            "run mypy --strict src tests tools",
        ]
