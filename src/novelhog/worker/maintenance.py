"""Worker housekeeping: emptying the storage trash and periodic maintenance (spec section 6.1)."""

from dataclasses import dataclass
from datetime import datetime

from novelhog import log
from novelhog.domain.tasks import TASK_RETENTION
from novelhog.repo import Repo
from novelhog.storage import InvalidKey, StaticStorage


@dataclass(frozen=True)
class MaintenanceReport:
    """What one `maintain` run did: when, events pruned, consumers moved forward, tasks pruned.

    Each moved consumer is `(name, old last_seq, new last_seq, skipped_releases)`.
    """

    at: datetime
    deleted: int
    consumers_moved: list[tuple[str, int, int, int]]
    tasks_pruned: int = 0


def empty_trash(repo: Repo, storage: StaticStorage, *, limit: int = 100) -> int:
    """Delete up to `limit` trashed files and forget their keys; return how many keys were handled.

    A key whose file is already gone counts as deleted. A key the storage rejects can never name
    a file, so it is forgotten with a warning rather than blocking the trash forever.
    """
    keys = repo.trash.take(limit)
    for key in keys:
        try:
            storage.delete(key)
        except InvalidKey:
            log.get().warning("trash_key_invalid", key=key)
    repo.trash.remove(keys)
    return len(keys)


def maintain(repo: Repo, *, now: datetime) -> MaintenanceReport:
    """Prune the event log, warn about moved consumers, prune finished tasks, then optimize.

    Finished tasks are kept for `TASK_RETENTION` after they finished (tasks spec section 8).
    """
    pruned = repo.events.prune()
    logger = log.get()
    for name, old_seq, new_seq, skipped_releases in pruned.consumers_moved:
        logger.warning(
            "consumer_moved_forward",
            consumer=name,
            skipped_from_seq=old_seq + 1,
            skipped_to_seq=new_seq,
            skipped_releases=skipped_releases,
        )
    tasks_pruned = repo.tasks.prune(now - TASK_RETENTION)
    repo.optimize()
    return MaintenanceReport(
        at=now,
        deleted=pruned.deleted,
        consumers_moved=pruned.consumers_moved,
        tasks_pruned=tasks_pruned,
    )
