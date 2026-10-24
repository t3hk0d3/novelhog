"""Tasks routes (tasks spec section 7): read-only."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from novelhog.api.deps import get_repo
from novelhog.api.schemas import ErrorBody, Task, TaskList
from novelhog.domain.errors import ValidationFailed
from novelhog.domain.tasks import TaskKind, TaskState
from novelhog.repo import Repo
from novelhog.repo.tasks import DEFAULT_LIMIT, MAX_LIMIT

router = APIRouter()

RepoDep = Annotated[Repo, Depends(get_repo)]

_BODY = "[a-z0-9]{12}"
_MAX_INT = 2**63 - 1  # signed 64-bit: larger values overflow the database
_ERRORS: dict[int | str, dict[str, Any]] = {404: {"model": ErrorBody}}


@router.get(
    "/tasks",
    operation_id="list_tasks",
    response_model=TaskList,
    responses={422: {"model": ErrorBody}},
)
def list_tasks(  # noqa: PLR0913  (the query parameters of the route)
    repo: RepoDep,
    novel_id: Annotated[str | None, Query(pattern=f"^NV{_BODY}$")] = None,
    entity_id: Annotated[str | None, Query(pattern=f"^(NV|CH|EX){_BODY}$")] = None,
    kind: Annotated[list[TaskKind] | None, Query()] = None,
    state: Annotated[list[TaskState] | None, Query()] = None,
    since: Annotated[int | None, Query(ge=0, le=_MAX_INT)] = None,
    offset: Annotated[int | None, Query(ge=0, le=_MAX_INT)] = None,
    limit: Annotated[int | None, Query(ge=1, le=MAX_LIMIT)] = None,
) -> TaskList:
    """Return tasks in claim order; with `since`, every live task and every change since then."""
    if since is not None and (offset is not None or limit is not None):
        raise ValidationFailed("validation_error", "offset and limit cannot be combined with since")
    page = repo.tasks.list(
        novel_id=novel_id,
        entity_id=entity_id,
        kinds=kind or (),
        states=state or (),
        since=since,
        offset=offset or 0,
        limit=DEFAULT_LIMIT if limit is None else limit,
    )
    return TaskList(
        items=[Task.model_validate(view.to_json()) for view in page.items],
        total=page.total,
        next_since=page.next_since,
    )


@router.get(
    "/tasks/{task_id}",
    operation_id="get_task",
    response_model=Task,
    responses=_ERRORS,
)
def get_task(task_id: str, repo: RepoDep) -> Task:
    """Return one task; 404 for an unknown, malformed or pruned ID."""
    return Task.model_validate(repo.tasks.get(task_id).to_json())
