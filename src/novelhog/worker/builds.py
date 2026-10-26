"""Book input for export builds (spec section 6.5): stored chapters and cover, read for a build.

The build itself runs as an `export_build` task (`worker/tasks/export_build.py`).
"""

from novelhog import log
from novelhog.chapter_store import ChapterStore
from novelhog.exporter import BookChapter, BookInput
from novelhog.repo.exports import ExportClaim
from novelhog.storage import StaticStorage
from novelhog.worker.errors import summarize

RANGE_CHANGED = "Range changed upstream"
_DEFAULT_LANGUAGE = "en"
_COVER_MIMES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


class RangeChangedError(Exception):
    """An end chapter of the range lost its position after the build started."""


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


def book_input(
    store: ChapterStore, storage: StaticStorage, claim: ExportClaim, ids: list[str]
) -> BookInput:
    """Read titles and positions in one go; chapters removed since the start are left out.

    Raises `RangeChangedError` when an end chapter lost its position meanwhile.
    """
    headings = store.headings(ids)
    if not ids or ids[0] not in headings or ids[-1] not in headings:
        raise RangeChangedError
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
