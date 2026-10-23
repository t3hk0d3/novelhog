"""Exports routes (spec section 5.5)."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response

from novelhog.api.deps import get_repo, path_id
from novelhog.api.schemas import ChapterRange, ErrorBody, Export, ExportCreate
from novelhog.domain.errors import ValidationFailed
from novelhog.domain.exports import Format
from novelhog.repo import Repo

router = APIRouter()

NovelId = Annotated[str, Depends(path_id("NV"))]
ExportId = Annotated[str, Depends(path_id("EX", "export_id"))]
RepoDep = Annotated[Repo, Depends(get_repo)]

_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorBody}}


@router.get(
    "/novels/{novel_id}/exports",
    operation_id="list_exports",
    response_model=list[Export],
    responses=_NOT_FOUND,
)
def list_exports(novel_id: NovelId, repo: RepoDep) -> list[Export]:
    """Return the novel's exports, newest first."""
    return [Export.model_validate(v.to_json()) for v in repo.exports.list(novel_id)]


@router.post(
    "/novels/{novel_id}/exports",
    operation_id="create_export",
    response_model=Export,
    status_code=201,
    responses={
        200: {"model": Export, "description": "An identical export is still active"},
        **_NOT_FOUND,
        422: {"model": ErrorBody},
    },
)
def create_export(
    novel_id: NovelId, body: ExportCreate, repo: RepoDep, response: Response
) -> Export:
    """Create an export of a chapter range, or return the identical active one with 200."""
    if not isinstance(body.chapters, ChapterRange):
        raise ValidationFailed("invalid_selector", "An export takes a range, not a list")
    try:
        fmt = Format(body.format)
    except ValueError:
        raise ValidationFailed(
            "unsupported_format", f"Unsupported export format: {body.format}"
        ) from None
    view, created = repo.exports.create(
        novel_id, body.chapters.start, body.chapters.end, fmt, skip_missing=body.skip_missing
    )
    if not created:
        response.status_code = 200
    return Export.model_validate(view.to_json())


@router.delete(
    "/novels/{novel_id}/exports/{export_id}",
    operation_id="delete_export",
    status_code=204,
    response_class=Response,
    responses={**_NOT_FOUND, 409: {"model": ErrorBody}},
)
def delete_export(novel_id: NovelId, export_id: ExportId, repo: RepoDep) -> Response:
    """Delete an export in any state except building."""
    repo.exports.delete(novel_id, export_id)
    return Response(status_code=204)
