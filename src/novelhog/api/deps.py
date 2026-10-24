"""Shared FastAPI dependencies."""

import inspect
from collections.abc import Callable
from typing import cast

from fastapi import Path, Request

from novelhog import ids
from novelhog.config import Settings
from novelhog.domain.errors import NotFound
from novelhog.repo import Repo


def get_repo(request: Request) -> Repo:
    """Return the app's repository."""
    repo = cast("Repo | None", request.app.state.repo)
    if repo is None:
        msg = "this app was built without a database"
        raise RuntimeError(msg)
    return repo


def get_settings(request: Request) -> Settings:
    """Return the app's configuration."""
    return cast("Settings", request.app.state.settings)


def path_id(prefix: str, name: str = "novel_id") -> Callable[..., str]:
    """Build a dependency for the path parameter `name` that accepts only `prefix` identifiers.

    A malformed or wrong-prefix identifier raises `NotFound`, which maps to `404 not_found`.
    """
    entities = {"NV": "novel", "CH": "chapter", "EX": "export", "SB": "subscription"}

    def dependency(**params: str) -> str:
        value = params[name]
        if not ids.is_valid(value, prefix):
            raise NotFound(entities.get(prefix, "resource"), value)
        return value

    # FastAPI reads the parameter name and its Path() declaration from the signature.
    dependency.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter(
                name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=str,
                default=Path(),
            )
        ],
        return_annotation=str,
    )
    return dependency
