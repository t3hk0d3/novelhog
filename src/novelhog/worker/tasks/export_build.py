"""The worker half of `export_build`: v1's build without the claim (v1 spec 6.5, tasks spec 5).

`run` writes the book to a temporary file and stores it under the export's key, then hands the
result to the repo handler: `Done(BuildResult)`, or `Failed` with "Range changed upstream" when an
end chapter lost its position, or with '<Type>: <summary>' for any other failure (logged with its
traceback as `export_build_failed`). There is no `Retry`: as in v1, any error fails the export.
"""

import contextlib
import uuid
from typing import Any, ClassVar

from novelhog import exporter, log
from novelhog.domain.exports import FORMATS, export_key
from novelhog.domain.tasks import Done, Failed, Outcome, TaskKind
from novelhog.repo.exports import ExportClaim
from novelhog.repo.task_kinds.export_build import BuildReport, BuildResult
from novelhog.repo.tasks import TaskRow
from novelhog.worker.builds import RANGE_CHANGED, RangeChangedError, book_input
from novelhog.worker.errors import summarize
from novelhog.worker.tasks.base import TaskContext

CLAIM_LOST = "Claim lost before upload"


class ExportBuildProcessor:
    """Builds one export into static storage without holding a transaction."""

    kind: ClassVar[TaskKind] = TaskKind.EXPORT_BUILD

    def run(self, task: TaskRow, work: ExportClaim, ctx: TaskContext) -> Outcome[BuildResult]:
        """Build and store the book; every failure becomes `Failed`, none is raised."""
        claim = work
        logger = log.get(export_id=claim.export_id, novel_id=claim.novel_id)
        tmp_dir = ctx.settings.data_dir / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        # Unique per attempt: a worker that lost the claim may still run the same export.
        temp = tmp_dir / f"{claim.export_id}.{uuid.uuid4().hex}.{FORMATS[claim.fmt].extension}"
        try:
            if claim.chapter_ids is None:
                return Failed(RANGE_CHANGED)
            try:
                book = book_input(ctx.chapter_store, ctx.storage, claim, claim.chapter_ids)
            except RangeChangedError:
                return Failed(RANGE_CHANGED)
            exporter.build(claim.fmt, book, temp)
            if ctx.beat is not None and ctx.beat.lost:
                logger.warning("export_claim_lost", stage="before_upload")
                return Failed(CLAIM_LOST)
            key = export_key(claim.novel_id, claim.export_id, claim.filename)
            size = temp.stat().st_size
            ctx.storage.put_file(key, temp, FORMATS[claim.fmt].mime, claim.filename)
            return Done(BuildResult(key, size))
        except Exception as error:  # noqa: BLE001 - any build failure fails the export
            logger.error("export_build_failed", error=summarize(error), exc_info=True)
            return Failed(summarize(error))
        finally:
            with contextlib.suppress(FileNotFoundError):
                temp.unlink()

    def stored_keys(self, outcome: Outcome[BuildResult]) -> list[str]:
        """The stored book's key, if `run` stored one."""
        if isinstance(outcome, Done):
            return [outcome.result.storage_key]
        return []

    def finished(self, task: TaskRow, outcome: Outcome[Any], report: object) -> None:
        """Warn when the export was no longer building, so the result was not applied."""
        if isinstance(report, BuildReport) and report.state is None:
            log.get(export_id=report.export_id, novel_id=task.novel_id).warning(
                "export_not_building"
            )
