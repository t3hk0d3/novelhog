"""Unit tests for novelhog.log."""

import json
from typing import Any

import pytest

from novelhog import log


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _fail() -> None:
    raise RuntimeError("boom")


class TestSetup__OnLogging:
    def test_setup__writes_one_json_object_with_standard_fields(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log.setup("worker")
        log.get().info("hello")
        (line,) = _lines(capsys)
        assert line["event"] == "hello"
        assert line["level"] == "info"
        assert line["service"] == "worker"
        assert "timestamp" in line


class TestGet__WithBoundFields:
    def test_get__includes_bound_fields_in_every_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log.setup("api")
        logger = log.get(novel_id="NVabc", chapter_id="CHdef", worker_id="w:1", host="h.example")
        logger.info("one")
        logger.warning("two")
        lines = _lines(capsys)
        assert len(lines) == 2
        for line in lines:
            assert line["novel_id"] == "NVabc"
            assert line["chapter_id"] == "CHdef"
            assert line["worker_id"] == "w:1"
            assert line["host"] == "h.example"


class TestGet__OnException:
    def test_get__includes_traceback_in_the_json_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log.setup("cli")
        try:
            _fail()
        except RuntimeError:
            log.get().exception("failed")
        (line,) = _lines(capsys)
        assert "Traceback" in line["exception"]
        assert "RuntimeError: boom" in line["exception"]
