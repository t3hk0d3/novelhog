"""Per-kind task handlers, the transactional half of each task kind (tasks spec section 5)."""

from novelhog.repo.context import RepoContext
from novelhog.repo.task_kinds.base import HandlerRegistry, TaskHandler

__all__ = ["HandlerRegistry", "TaskHandler", "default_handlers"]


def default_handlers(context: RepoContext) -> HandlerRegistry:
    """The handlers `Repo` registers when none are given; one per kind as kinds move to tasks."""
    # Imported here: the handlers use `TaskQueue`, whose module imports this package.
    from novelhog.repo.task_kinds.novel_refresh import NovelRefreshHandler  # noqa: PLC0415

    handler = NovelRefreshHandler(context)
    return {handler.kind: handler}
