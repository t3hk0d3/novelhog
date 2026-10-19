"""Hosts and novels routes (spec section 5.3)."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response

from novelhog.api.deps import get_repo, path_id
from novelhog.api.schemas import ErrorBody, Host, Novel, NovelCreate, NovelPatch
from novelhog.domain.payloads import NovelView
from novelhog.repo import Repo

router = APIRouter()

NovelId = Annotated[str, Depends(path_id("NV"))]
RepoDep = Annotated[Repo, Depends(get_repo)]

_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorBody}}


def _novel(view: NovelView) -> Novel:
    return Novel.model_validate(view.to_json())


@router.get("/hosts", operation_id="list_hosts", response_model=list[Host])
def list_hosts(repo: RepoDep) -> list[Host]:
    """Return the supported hosts."""
    return [Host(host=h.host) for h in repo.hosts.list_supported()]


@router.get("/novels", operation_id="list_novels", response_model=list[Novel])
def list_novels(repo: RepoDep) -> list[Novel]:
    """Return all novels, newest first."""
    return [_novel(view) for view in repo.novels.list()]


@router.post(
    "/novels",
    operation_id="create_novel",
    response_model=Novel,
    status_code=201,
    responses={409: {"model": ErrorBody}, 422: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)
def create_novel(body: NovelCreate, repo: RepoDep) -> Novel:
    """Create a novel from a TOC URL; the worker imports it later."""
    return _novel(repo.novels.create(body.toc_url, body.followed))


@router.get(
    "/novels/{novel_id}", operation_id="get_novel", response_model=Novel, responses=_NOT_FOUND
)
def get_novel(novel_id: NovelId, repo: RepoDep) -> Novel:
    """Return one novel."""
    return _novel(repo.novels.get(novel_id))


@router.patch(
    "/novels/{novel_id}",
    operation_id="patch_novel",
    response_model=Novel,
    responses={**_NOT_FOUND, 422: {"model": ErrorBody}},
)
def patch_novel(novel_id: NovelId, body: NovelPatch, repo: RepoDep) -> Novel:
    """Change `followed`, `seen` or `title`."""
    view = repo.novels.update(novel_id, followed=body.followed, seen=body.seen, title=body.title)
    return _novel(view)


@router.delete(
    "/novels/{novel_id}",
    operation_id="delete_novel",
    status_code=204,
    response_class=Response,
    responses=_NOT_FOUND,
)
def delete_novel(novel_id: NovelId, repo: RepoDep) -> Response:
    """Delete the novel with its chapters, contents and exports."""
    repo.novels.delete(novel_id)
    return Response(status_code=204)


@router.post(
    "/novels/{novel_id}/sync",
    operation_id="sync_novel",
    response_model=Novel,
    status_code=202,
    responses=_NOT_FOUND,
)
def sync_novel(novel_id: NovelId, repo: RepoDep) -> Novel:
    """Ask for an immediate sync."""
    return _novel(repo.novels.request_sync(novel_id))
