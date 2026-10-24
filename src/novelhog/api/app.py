"""FastAPI application factory, lifespan and error handlers (spec sections 5 and 11)."""

import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Any, cast
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from novelhog import log, vapid
from novelhog.api import (
    routes_chapters,
    routes_events,
    routes_exports,
    routes_novels,
    routes_settings,
    routes_subscriptions,
    routes_tasks,
)
from novelhog.api.checks import OriginChecks
from novelhog.config import Settings
from novelhog.domain.errors import (
    Conflict,
    DomainError,
    NotFound,
    ServiceUnavailable,
    ValidationFailed,
)
from novelhog.repo import Busy, Repo

API_PREFIX = "/api/v1"


def _snake(phrase: str) -> str:
    return phrase.lower().replace(" ", "_").replace("-", "_").replace("'", "")


def _document_validation_errors(app: FastAPI) -> None:
    """Make the OpenAPI 422 responses describe `ErrorBody`, the shape actually returned."""
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            document = generate()
            ref = {"$ref": "#/components/schemas/ErrorBody"}
            for path in document["paths"].values():
                for operation in path.values():
                    content = operation.get("responses", {}).get("422", {}).get("content")
                    if content:
                        content["application/json"]["schema"] = ref
            schemas = document["components"]["schemas"]
            schemas.pop("HTTPValidationError", None)
            schemas.pop("ValidationError", None)
        return app.openapi_schema or {}

    app.openapi = openapi  # type: ignore[method-assign]


def _error(
    status: int,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, object] = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return JSONResponse(status_code=status, content={"error": body}, headers=headers)


def _register_error_handlers(app: FastAPI) -> None:
    def domain_handler(status: int) -> Callable[[Request, Exception], Awaitable[JSONResponse]]:
        async def handle(_request: Request, exc: Exception) -> JSONResponse:
            error = cast("DomainError", exc)
            return _error(status, error.code, error.message, error.details)

        return handle

    app.add_exception_handler(NotFound, domain_handler(404))
    app.add_exception_handler(Conflict, domain_handler(409))
    app.add_exception_handler(ValidationFailed, domain_handler(422))
    app.add_exception_handler(ServiceUnavailable, domain_handler(503))

    async def busy(_request: Request, _exc: Exception) -> JSONResponse:
        return _error(
            503,
            "database_busy",
            "The database is busy, try again shortly",
            headers={"Retry-After": "2"},
        )

    async def invalid_request(_request: Request, exc: Exception) -> JSONResponse:
        failure = cast("RequestValidationError", exc)
        errors: list[object] = [
            {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in failure.errors()
        ]
        return _error(422, "validation_error", "The request is not valid", {"errors": errors})

    async def http_error(_request: Request, exc: Exception) -> JSONResponse:
        failure = cast("StarletteHTTPException", exc)
        status = failure.status_code
        phrase = HTTPStatus(status).phrase if status in HTTPStatus else "Error"
        code = "not_found" if status == 404 else _snake(phrase)
        message = phrase if not isinstance(failure.detail, str) else failure.detail
        return _error(status, code, message, headers=dict(failure.headers or {}))

    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.get().error(
            "unhandled_exception",
            method=request.method,
            path=request.url.path,
            exc_info=exc,
        )
        return _error(500, "internal_error", "Internal server error")

    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(Busy, busy)
    app.add_exception_handler(RequestValidationError, invalid_request)
    app.add_exception_handler(Exception, unexpected)


def create_app(
    settings: Settings,
    *,
    repo: Repo | None = None,
    check_schema: bool = True,
    sse_poll_s: float = 1.0,
    sse_keepalive_s: float = 15.0,
) -> FastAPI:
    """Build the API app.

    With `check_schema` the lifespan refuses to start on an outdated database. Without a `repo`
    one is built from `settings` when `check_schema` is true; `check_schema=False` with no
    `repo` builds a database-free app (used to export the OpenAPI document).
    """
    owned_repo = repo is None and check_schema
    if owned_repo:
        repo = Repo(
            settings.database_url,
            static_base_url=settings.static_base_url,
            refresh_min_interval=settings.refresh_min_interval,
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        app.state.stopping.clear()  # the same app may be started again
        try:
            if repo is not None:
                if check_schema:
                    repo.require_current()
                if settings.webpush_enabled:
                    vapid.ensure_keys(repo)
            yield
        finally:
            app.state.stopping.set()
            if owned_repo and repo is not None:
                repo.engine.dispose()

    app = FastAPI(
        title="novelhog",
        lifespan=lifespan,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.repo = repo
    app.state.stopping = threading.Event()
    app.state.sse_poll_s = sse_poll_s
    app.state.sse_keepalive_s = sse_keepalive_s

    public_host = urlsplit(settings.public_url).hostname if settings.public_url else None
    app.add_middleware(OriginChecks, public_host=public_host, allowed_hosts=settings.allowed_hosts)
    _register_error_handlers(app)
    _document_validation_errors(app)
    app.include_router(routes_settings.router, prefix=API_PREFIX)
    app.include_router(routes_novels.router, prefix=API_PREFIX)
    app.include_router(routes_chapters.router, prefix=API_PREFIX)
    app.include_router(routes_exports.router, prefix=API_PREFIX)
    app.include_router(routes_subscriptions.router, prefix=API_PREFIX)
    app.include_router(routes_events.router, prefix=API_PREFIX)
    app.include_router(routes_tasks.router, prefix=API_PREFIX)
    return app
