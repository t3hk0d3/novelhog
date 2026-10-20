"""Export builds (spec section 6.5): chapters and cover into a file in static storage."""

import contextlib
from pathlib import Path

from novelhog import exporter, log
from novelhog.chapter_store import ChapterStore
from novelhog.config import Settings
from novelhog.domain.exports import FORMATS, export_key
from novelhog.exporter import BookChapter, BookInput
from novelhog.repo import Repo
from novelhog.repo.exports import ExportClaim
from novelhog.storage import StaticStorage
from novelhog.worker.runtime import Heartbeat

_SUMMARY_LIMIT = 300
_DEFAULT_LANGUAGE = "en"
_COVER_MIMES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def _summary(error: Exception) -> str:
    text = " ".join(str(error).split())[:_SUMMARY_LIMIT]
    return f"{type(error).__name__}: {text}"


def _cover(storage: StaticStorage, claim: ExportClaim) -> tuple[bytes, str] | None:
    """The stored cover and its mime type; any problem is logged and gives no cover."""
    if claim.cover_key is None:
        return None
    try:
        data = storage.get(claim.cover_key)
    except OSError as error:
        log.get(export_id=claim.export_id).warning("export_cover_failed", error=_summary(error))
        return None
    if data is None:
        log.get(export_id=claim.export_id).warning(
            "export_cover_missing", cover_key=claim.cover_key
        )
        return None
    extension = claim.cover_key.rsplit(".", 1)[-1].lower()
    return data, _COVER_MIMES.get(extension, "application/octet-stream")


def _book(
    repo: Repo, store: ChapterStore, storage: StaticStorage, claim: ExportClaim, ids: list[str]
) -> BookInput:
    chapters = []
    for chapter_id in ids:
        chapter = repo.chapters.get(chapter_id)
        position = chapter.position
        assert position is not None  # noqa: S101 - `chapters.get` raises for removed chapters
        chapters.append(
            BookChapter(position, chapter.title or f"Chapter {position}", store.get(chapter_id))
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
    temp = tmp_dir / f"{claim.export_id}.{FORMATS[claim.fmt].extension}"
    try:
        with Heartbeat(lambda: repo.exports.renew(claim.export_id, worker_id, ttl), ttl / 3):
            if claim.chapter_ids is None:
                repo.exports.fail(claim.export_id, worker_id, "Range changed upstream")
                return
            book = _book(repo, store, storage, claim, claim.chapter_ids)
            exporter.build(claim.fmt, book, temp)
            key = export_key(claim.novel_id, claim.export_id, claim.filename)
            storage.put_file(key, temp, FORMATS[claim.fmt].mime, claim.filename)
            size = temp.stat().st_size
            if not repo.exports.finish(
                claim.export_id, worker_id, storage_key=key, size_bytes=size
            ):
                repo.trash.discard([key])
                logger.warning("export_claim_lost", storage_key=key)
    except Exception as error:  # noqa: BLE001 - any build failure fails the export, not the worker
        logger.error("export_build_failed", error=_summary(error), exc_info=True)
        if not repo.exports.fail(claim.export_id, worker_id, _summary(error)):
            logger.warning("export_claim_lost")
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()
