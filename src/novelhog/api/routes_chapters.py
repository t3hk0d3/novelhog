"""Chapters routes (spec section 5.4)."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from novelhog.api.deps import get_repo, path_id
from novelhog.api.schemas import (
    Chapter,
    ChapterPage,
    ChapterPatch,
    ChapterPatchResult,
    ChapterRange,
    ChapterSkipped,
    ErrorBody,
)
from novelhog.domain import chapters as domain
from novelhog.repo import Repo
from novelhog.repo.chapters import MAX_LIMIT

router = APIRouter()

NovelId = Annotated[str, Depends(path_id("NV"))]
RepoDep = Annotated[Repo, Depends(get_repo)]

_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorBody}}


@router.get(
    "/novels/{novel_id}/chapters",
    operation_id="list_chapters",
    response_model=ChapterPage,
    responses={**_NOT_FOUND, 422: {"model": ErrorBody}},
)
def list_chapters(
    novel_id: NovelId,
    repo: RepoDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=0, le=MAX_LIMIT)] = 1000,
    state: domain.ChapterState | None = None,
) -> ChapterPage:
    """Return a page of the novel's chapters ordered by position."""
    views, total = repo.chapters.list(novel_id, offset=offset, limit=limit, state=state)
    return ChapterPage(items=[Chapter.model_validate(v.to_json()) for v in views], total=total)


@router.patch(
    "/novels/{novel_id}/chapters",
    operation_id="patch_chapters",
    response_model=ChapterPatchResult,
    responses={**_NOT_FOUND, 422: {"model": ErrorBody}},
)
def patch_chapters(novel_id: NovelId, body: ChapterPatch, repo: RepoDep) -> ChapterPatchResult:
    """Queue or dequeue the selected chapters; chapters that cannot move are skipped."""
    selector: domain.Selector
    if isinstance(body.chapters, ChapterRange):
        selector = domain.ChapterRange(body.chapters.start, body.chapters.end)
    else:
        selector = body.chapters
    result = repo.chapters.patch(novel_id, body.state, selector)
    return ChapterPatchResult(
        updated=result.updated,
        skipped=[ChapterSkipped(position=p, state=s) for p, s in result.skipped],
    )
