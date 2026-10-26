"""Shared configuration handed to every sub-repository."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine


@dataclass(frozen=True)
class RepoContext:
    """The engine, clock and settings every sub-repository needs; new fields go here."""

    engine: Engine
    clock: Callable[[], datetime]
    static_base_url: str
    refresh_min_interval: int
    refresh_max_interval: int
