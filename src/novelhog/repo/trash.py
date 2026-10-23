"""The storage trash: static-file keys waiting for the worker to delete them (spec section 4)."""

from datetime import datetime

from sqlalchemy import Connection, delete, select

from novelhog.domain.exports import ExportState, novel_id_of_export_key
from novelhog.repo import db
from novelhog.repo.context import RepoContext
from novelhog.repo.schema import exports, storage_trash
from novelhog.repo.sql import dialect_insert


def add(conn: Connection, keys: list[str], now: datetime) -> None:
    """Trash the keys inside the caller's transaction; a key already trashed is left alone."""
    if not keys:
        return
    statement = dialect_insert(conn.engine, storage_trash).on_conflict_do_nothing(
        index_elements=["key"]
    )
    conn.execute(statement, [{"key": key, "at": now} for key in dict.fromkeys(keys)])


def _referenced(conn: Connection, keys: list[str]) -> set[str]:
    """The keys among `keys` that a `ready` export references."""
    return set(
        conn.execute(
            select(exports.c.storage_key).where(
                exports.c.storage_key.in_(keys), exports.c.state == ExportState.READY.value
            )
        ).scalars()
    )


class Trash:
    """The worker's side of the trash: take keys, delete the files, remove the keys."""

    def __init__(self, context: RepoContext) -> None:
        self._engine = context.engine
        self._clock = context.clock

    def discard(self, keys: list[str]) -> None:
        """Trash the keys in their own transaction (a file uploaded but never published).

        Keys that a `ready` export references are skipped: export keys are deterministic, so a
        build that lost its claim may have written the very file the new owner published. To
        serialise with that owner's `finish`, the transaction first takes the lock of the novel
        named in each export key (`exports/<novel_id>/...`, the same lock `finish` takes). A key
        of another shape is trashed without that lock; so is one of a deleted novel, which no
        export can reference any more.
        """
        from novelhog.repo.novels import lock_novel  # noqa: PLC0415 - novels imports this module

        if not keys:
            return
        unique = list(dict.fromkeys(keys))
        with db.write(self._engine) as conn:
            novel_ids = {n for key in unique if (n := novel_id_of_export_key(key)) is not None}
            for novel_id in sorted(novel_ids):
                lock_novel(conn, novel_id)
            live = _referenced(conn, unique)
            add(conn, [key for key in unique if key not in live], self._clock())

    def take(self, limit: int = 100) -> list[str]:
        """Return up to `limit` trashed keys, oldest first; they stay until `remove`.

        Never returns a key a `ready` export still references; such trash rows are deleted.
        """
        with db.write(self._engine) as conn:
            rows = list(
                conn.execute(
                    select(storage_trash.c.key)
                    .order_by(storage_trash.c.at, storage_trash.c.key)
                    .limit(limit)
                ).scalars()
            )
            live = _referenced(conn, rows)
            if live:
                conn.execute(delete(storage_trash).where(storage_trash.c.key.in_(live)))
            return [key for key in rows if key not in live]

    def remove(self, keys: list[str]) -> None:
        """Forget keys whose files were deleted."""
        if not keys:
            return
        with db.write(self._engine) as conn:
            conn.execute(delete(storage_trash).where(storage_trash.c.key.in_(keys)))
