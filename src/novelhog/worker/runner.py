"""The task runner: claims one task at a time and runs it (tasks spec section 6).

`Runner.step` claims the next task and hands it to `run_task`:

1. the processor of the task's kind is looked up; a kind without one is a programming error,
   so the claim is released and `UnregisteredTaskKindError` raised (the loop logs it as
   `worker_step_failed`); the same happens when `repo.tasks` has no handler for the kind;
2. `_wait_turn` runs before the task starts; on stop the claimed task is released instead
   (politeness per host hooks in here in a later task);
3. `repo.tasks.start` starts it and runs the handler's `begin`; a task that was not started or
   is no longer needed is logged and left;
4. the processor's `run` works under a heartbeat that renews the claim every `CLAIM_TTL / 3`; an
   exception is recorded as `Failed('<Type>: <summary>')`;
5. `repo.tasks.finish` records the outcome; when the fence fails the result is discarded and
   the files `run` stored are trashed.

A `Retry` outcome, or a start or finish transaction that itself fails, raises `RunnerBackoff`
so the loop backs off; the task then keeps its claim until recovery puts it back.
Every transition logs one `task_<verb>` line with `task_id`, `kind`, `entity_id` and `attempts`.
"""

from collections.abc import Mapping
from typing import Any

from structlog.typing import FilteringBoundLogger

from novelhog import log
from novelhog.domain.tasks import Done, Failed, Outcome, Retry, StartResult, TaskKind
from novelhog.repo import Repo
from novelhog.repo.tasks import (
    NotStarted,
    Ready,
    Skipped,
    TaskRecoveryReport,
    TaskRow,
    UnregisteredTaskKindError,
)
from novelhog.worker.errors import summarize
from novelhog.worker.tasks.base import TaskContext, TaskProcessor

_NOT_STARTED = {
    StartResult.CANCELLED: "task_cancelled",
    StartResult.DEFERRED: "task_deferred",
    StartResult.LOST: "task_lost",
}


class RunnerBackoff(Exception):  # noqa: N818  (a signal to the loop, not an error to report)
    """The loop should back off: a task asked for a retry or its transaction failed.

    The runner has already logged the task event; the loop only waits.
    """


def _task_logger(worker_id: str, task: TaskRow) -> FilteringBoundLogger:
    return log.get(worker_id=worker_id).bind(
        task_id=task.id, kind=task.kind.value, entity_id=task.entity_id, attempts=task.attempts
    )


def _outcome_name(outcome: Outcome[Any]) -> str:
    return "done" if isinstance(outcome, Done) else "failed"


class Runner:
    """Claims and runs tasks one at a time with the registered processors."""

    def __init__(
        self,
        repo: Repo,
        processors: Mapping[TaskKind, TaskProcessor[Any, Any]],
        ctx: TaskContext,
    ) -> None:
        for kind, processor in processors.items():
            if processor.kind is not kind:
                message = f"processor for {processor.kind.value} registered as {kind.value}"
                raise ValueError(message)
        self._repo = repo
        self._processors = dict(processors)
        self._ctx = ctx
        self._worker_id = ctx.settings.worker_id
        self._ttl = ctx.settings.claim_ttl

    def recover(self) -> TaskRecoveryReport:
        """Recover tasks whose claim lapsed and log `task_recovered` for each running one."""
        report = self._repo.tasks.recover()
        for task in report.recovered:
            _task_logger(self._worker_id, task).info("task_recovered")
        return report

    def step(self) -> bool:
        """Claim and run one task; False when nothing was claimable.

        Raises `RunnerBackoff` after a `Retry` or a failed start or finish transaction.
        """
        task = self._repo.tasks.claim(self._worker_id, self._ttl)
        if task is None:
            return False
        self.run_task(task)
        return True

    def run_task(self, task: TaskRow) -> None:
        """Start, run and finish one claimed task (tasks spec section 6, `run_task`)."""
        processor = self._processors.get(task.kind)
        if processor is None:
            self._repo.tasks.release(task, self._worker_id)
            raise UnregisteredTaskKindError(task.kind, "processor")
        if not self._wait_turn(task):
            self._repo.tasks.release(task, self._worker_id)
            return
        logger = _task_logger(self._worker_id, task)
        try:
            started = self._repo.tasks.start(task, self._worker_id)
        except UnregisteredTaskKindError:
            self._repo.tasks.release(task, self._worker_id)
            raise
        except Exception as error:
            logger.error("task_error", stage="start", error=summarize(error), exc_info=True)
            raise RunnerBackoff from error
        if isinstance(started, NotStarted):
            logger.info(_NOT_STARTED[started.result])
            return
        if isinstance(started, Skipped):
            logger.info("task_skipped", reason=started.reason)
            return
        self._run_started(processor, started)

    def _wait_turn(self, task: TaskRow) -> bool:
        """Wait until the task may start; False when the worker is stopping.

        The seam for per-host politeness (tasks spec section 6), which a later task adds.
        """
        return not self._ctx.stop.is_set()

    def _run_started(self, processor: TaskProcessor[Any, Any], started: Ready[Any]) -> None:
        task = started.task
        logger = _task_logger(self._worker_id, task)
        logger.info("task_started")
        raised: Exception | None = None
        with self._ctx.heartbeat(
            lambda: self._repo.tasks.renew(task, self._worker_id, self._ttl), self._ttl / 3
        ):
            try:
                outcome = processor.run(task, started.work, self._ctx)
            except Exception as error:  # noqa: BLE001 - any failure of the work fails the task
                raised = error
                outcome = Failed(summarize(error))
        try:
            finished = self._repo.tasks.finish(task, self._worker_id, outcome)
        except Exception as error:
            logger.error("task_error", stage="finish", error=summarize(error), exc_info=True)
            raise RunnerBackoff from error
        if finished is None:
            logger.warning("task_lost", exc_info=raised)
            self._repo.trash.discard(processor.stored_keys(outcome))
        elif isinstance(outcome, Retry):
            logger.warning("task_retry", run_after=outcome.run_after, error=outcome.error)
        else:
            logger.info(
                "task_finished",
                outcome=_outcome_name(outcome),
                duration_ms=finished.task.duration_ms,
                error=outcome.error if isinstance(outcome, Failed) else None,
                exc_info=raised,
            )
        if isinstance(outcome, Retry):
            raise RunnerBackoff
