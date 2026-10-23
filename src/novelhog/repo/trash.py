"""The storage trash: static-file keys waiting for the worker to delete them (spec section 4)."""

from datetime import datetime

from sqlalchemy import Connection, delete, select

from novelhog.domain.exports import ExportState
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


class Trash:
    """The worker's side of the trash: take keys, delete the files, remove the keys."""

    def __init__(self, context: RepoContext) -> None:
        self._engine = context.engine
        self._clock = context.clock

    def discard(self, keys: list[str]) -> None:
        """Trash the keys in their own transaction (a file uploaded but never published).

        Keys that a `ready` export references are skipped: export keys are deterministic, so a
        build that lost its claim may have written the very file the new owner published.
        """
        if not keys:
            return
        with db.write(self._engine) as conn:
            live = set(
                conn.execute(
                    select(exports.c.storage_key).where(
                        exports.c.storage_key.in_(keys),
                        exports.c.state == ExportState.READY.value,
                    )
                ).scalars()
            )
            add(conn, [key for key in keys if key not in live], self._clock())

    def take(self, limit: int = 100) -> list[str]:
        """Return up to `limit` trashed keys, oldest first; they stay until `remove`."""
        with db.read(self._engine) as conn:
            return list(
                conn.execute(
                    select(storage_trash.c.key)
                    .order_by(storage_trash.c.at, storage_trash.c.key)
                    .limit(limit)
                ).scalars()
            )

    def remove(self, keys: list[str]) -> None:
        """Forget keys whose files were deleted."""
        if not keys:
            return
        with db.write(self._engine) as conn:
            conn.execute(delete(storage_trash).where(storage_trash.c.key.in_(keys)))
