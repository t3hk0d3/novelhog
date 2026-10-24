"""Per-kind task processors, the network half of each task kind (tasks spec section 5)."""

from typing import Any

from novelhog.worker.tasks.base import ProcessorRegistry, TaskProcessor
from novelhog.worker.tasks.export_build import ExportBuildProcessor
from novelhog.worker.tasks.novel_refresh import NovelRefreshProcessor

__all__ = ["default_processors"]


def default_processors() -> ProcessorRegistry:
    """The processors the worker runs when none are given; one per kind as kinds move to tasks."""
    processors: list[TaskProcessor[Any, Any]] = [NovelRefreshProcessor(), ExportBuildProcessor()]
    return {processor.kind: processor for processor in processors}
