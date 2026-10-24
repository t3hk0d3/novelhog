"""Export builds (spec section 6.5): chapters and cover into a file in static storage."""

import contextlib
import uuid
from pathlib import Path
from typing import Any

from novelhog import exporter, log
from novelhog.chapter_store import ChapterStore
from novelhog.config import Settings
from novelhog.domain.exports import FORMATS, export_key
from novelhog.exporter import BookChapter, BookInput
from novelhog.repo import Repo
from novelhog.repo.exports import ExportClaim
from novelhog.storage import StaticStorage
from novelhog.worker.errors import summarize
from novelhog.worker.runtime import Heartbeat

_DEFAULT_LANGUAGE = "en"
_RANGE_CHANGED = "Range changed upstream"
_COVER_MIMES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def _cover(storage: StaticStorage, claim: ExportClaim) -> tuple[bytes, str] | None:
    """The stored cover and its mime type; any problem is logged and gives no cover."""
    if claim.cover_key is None:
        return None
    try:
        data = storage.get(claim.cover_key)
    except Exception as error:  # noqa: BLE001 - any storage problem means no cover
        log.get(export_id=claim.export_id).warning("export_cover_failed", error=summarize(error))
        return None
    if data is None:
        log.get(export_id=claim.export_id).warning(
            "export_cover_missing", cover_key=claim.cover_key
        )
        return None
    extension = claim.cover_key.rsplit(".", 1)[-1].lower()
    return data, _COVER_MIMES.get(extension, "application/octet-stream")


class _RangeChanged(Exception):  # noqa: N818 - internal signal, not an error
    """An end chapter of the range lost its position while the build ran."""


def _book(
    repo: Repo, store: ChapterStore, storage: StaticStorage, claim: ExportClaim, ids: list[str]
) -> BookInput:
    """Read titles and positions in one go; chapters removed since the claim are left out."""
    headings = repo.chapters.headings(ids)
    if not ids or ids[0] not in headings or ids[-1] not in headings:
        raise _RangeChanged
    chapters = []
    for chapter_id in sorted(headings, key=lambda i: headings[i][0]):
        position, title = headings[chapter_id]
        chapters.append(
            BookChapter(position, title or f"Chapter {position}", store.get(chapter_id))
        )
    return BookInput(
        identifier=claim.export_id,
        title=claim.novel_title or "Untitled",
        language=claim.language or _DEFAULT_LANGUAGE,
        cover=_cover(storage, claim),
        chapters=chapters,
    )


def build(  # noqa: PLR0913
    repo: Repo,
    store: ChapterStore,
    storage: StaticStorage,
    claim: ExportClaim,
    *,
    settings: Settings,
    tmp_dir: Path,
) -> None:
    """Build the claimed export and mark it `ready`, or `failed` with a short message.

    Takes no novel claim. The export claim is kept alive by a `Heartbeat`; when it was lost
    before `finish`, the uploaded file goes to the trash and the export is left to its new owner.
    """
    worker_id, ttl = settings.worker_id, settings.claim_ttl
    logger = log.get(export_id=claim.export_id, novel_id=claim.novel_id)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # Unique per attempt: a worker that lost the claim may still run the same export.
    temp = tmp_dir / f"{claim.export_id}.{uuid.uuid4().hex}.{FORMATS[claim.fmt].extension}"
    try:
        with Heartbeat(
            lambda: repo.exports.renew(claim.export_id, worker_id, ttl), ttl / 3
        ) as beat:
            if claim.chapter_ids is None:
                _fail_range_changed(repo, claim, worker_id, logger)
                return
            try:
                book = _book(repo, store, storage, claim, claim.chapter_ids)
            except _RangeChanged:
                _fail_range_changed(repo, claim, worker_id, logger)
                return
            exporter.build(claim.fmt, book, temp)
            if beat.lost:
                logger.warning("export_claim_lost", stage="before_upload")
                return
            key = export_key(claim.novel_id, claim.export_id, claim.filename)
            storage.put_file(key, temp, FORMATS[claim.fmt].mime, claim.filename)
            size = temp.stat().st_size
            if not repo.exports.finish(
                claim.export_id, worker_id, storage_key=key, size_bytes=size
            ):
                repo.trash.discard([key])
                logger.warning("export_claim_lost", storage_key=key)
    except Exception as error:  # noqa: BLE001 - any build failure fails the export, not the worker
        logger.error("export_build_failed", error=summarize(error), exc_info=True)
        if not repo.exports.fail(claim.export_id, worker_id, summarize(error)):
            logger.warning("export_claim_lost")
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()


def _fail_range_changed(repo: Repo, claim: ExportClaim, worker_id: str, logger: Any) -> None:
    if not repo.exports.fail(claim.export_id, worker_id, _RANGE_CHANGED):
        logger.warning("export_claim_lost")
