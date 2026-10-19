"""Tests for error handlers and path id validation."""

from collections.abc import Callable
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from novelhog.api.deps import path_id
from novelhog.domain.errors import Conflict, NotFound, ServiceUnavailable, ValidationFailed
from novelhog.repo import Busy


class Body(BaseModel):
    count: int


def _routes(app: FastAPI) -> None:
    @app.get("/t/not-found")
    def not_found() -> None:
        raise NotFound("novel", "NVabc")

    @app.get("/t/conflict")
    def conflict() -> None:
        raise Conflict("novel_exists", "Novel exists", {"novel_id": "NVabc"})

    @app.get("/t/invalid")
    def invalid() -> None:
        raise ValidationFailed("invalid_title", "Bad title")

    @app.get("/t/unavailable")
    def unavailable() -> None:
        raise ServiceUnavailable("hosts_not_published", "Not yet")

    @app.get("/t/busy")
    def busy() -> None:
        raise Busy

    @app.get("/t/boom")
    def boom() -> None:
        msg = "secret detail"
        raise RuntimeError(msg)

    @app.post("/t/body")
    def post_body(body: Body) -> Body:
        return body

    @app.get("/t/novels/{novel_id}")
    def get_novel(novel_id: Annotated[str, Depends(path_id("NV"))]) -> str:
        return novel_id


@pytest.fixture
def errors_client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client(app_hook=_routes)


class TestErrors__OnNotFound:
    def test_error__returns_404_not_found(self, errors_client: TestClient) -> None:
        response = errors_client.get("/t/not-found")
        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "not_found"
        assert error["message"]


class TestErrors__OnDomainErrors:
    @pytest.mark.parametrize(
        ("path", "status", "code"),
        [
            ("conflict", 409, "novel_exists"),
            ("invalid", 422, "invalid_title"),
            ("unavailable", 503, "hosts_not_published"),
        ],
    )
    def test_error__returns_status_and_domain_code(
        self, errors_client: TestClient, path: str, status: int, code: str
    ) -> None:
        response = errors_client.get(f"/t/{path}")
        assert response.status_code == status
        assert response.json()["error"]["code"] == code

    def test_error__includes_details(self, errors_client: TestClient) -> None:
        error = errors_client.get("/t/conflict").json()["error"]
        assert error["details"] == {"novel_id": "NVabc"}


class TestErrors__OnBusy:
    def test_error__returns_503_database_busy_with_retry_after(
        self, errors_client: TestClient
    ) -> None:
        response = errors_client.get("/t/busy")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "database_busy"
        assert response.headers["Retry-After"] == "2"


class TestErrors__OnInvalidBody:
    def test_error__returns_422_validation_error_in_error_shape(
        self, errors_client: TestClient
    ) -> None:
        response = errors_client.post("/t/body", json={"count": "x"})
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "validation_error"
        assert error["details"]["errors"][0]["loc"] == ["body", "count"]


class TestErrors__OnUnexpectedException:
    def test_error__returns_500_without_traceback(self, errors_client: TestClient) -> None:
        response = errors_client.get("/t/boom")
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "internal_error"
        assert "secret detail" not in response.text
        assert "Traceback" not in response.text

    def test_error__logs_the_traceback(
        self, errors_client: TestClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from novelhog import log

        log.setup("api")
        errors_client.get("/t/boom")
        out = capsys.readouterr().out
        assert "secret detail" in out
        assert "Traceback" in out


class TestPathId:
    @pytest.mark.parametrize(
        "value", ["bogus", "CHabcdefghijkl", "NVshort", "NVABCDEFGHIJKL", "NVabcdefghijklm"]
    )
    def test_path_id__malformed_or_wrong_prefix_returns_404(
        self, errors_client: TestClient, value: str
    ) -> None:
        response = errors_client.get(f"/t/novels/{value}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_path_id__valid_id_passes(self, errors_client: TestClient) -> None:
        assert errors_client.get("/t/novels/NVabcdefghij12").json() == "NVabcdefghij12"
