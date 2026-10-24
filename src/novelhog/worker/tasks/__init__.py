"""Per-kind task processors, the network half of each task kind (tasks spec section 5)."""

from novelhog.worker.tasks.base import ProcessorRegistry

__all__ = ["default_processors"]


def default_processors() -> ProcessorRegistry:
    """The processors the worker runs when none are given; one per kind as kinds move to tasks."""
    return {}
