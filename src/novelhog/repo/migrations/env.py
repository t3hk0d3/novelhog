"""Alembic environment: runs on the connection given by `db.migrate` (spec section 4).

SQLite migrations run with foreign keys off, are followed by `PRAGMA foreign_key_check`, and
foreign keys are switched back on afterwards.
"""

from typing import Any

from alembic import context
from sqlalchemy import Connection

from novelhog.repo.db import include_object
from novelhog.repo.schema import metadata


def _run(connection: Connection) -> None:
    sqlite = connection.dialect.name == "sqlite"
    raw: Any = connection.connection.dbapi_connection
    if sqlite:
        # Must happen before the migration transaction starts; it is a no-op inside one.
        raw.execute("PRAGMA foreign_keys = OFF")
    try:
        context.configure(
            connection=connection,
            target_metadata=metadata,
            compare_type=True,
            include_object=include_object,
            render_as_batch=sqlite,
            transactional_ddl=True,  # SQLite DDL is transactional: roll back as a whole
        )
        with context.begin_transaction():
            context.run_migrations()
            if sqlite:
                orphans = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                if orphans:
                    msg = f"foreign_key_check reported {len(orphans)} violation(s): {orphans[:5]}"
                    raise RuntimeError(msg)  # noqa: TRY301
    except BaseException:
        connection.rollback()
        raise
    finally:
        if sqlite:
            raw.execute("PRAGMA foreign_keys = ON")


connection = context.config.attributes.get("connection")
if connection is None:
    msg = "migrations run through novelhog.repo.db.migrate(engine)"
    raise RuntimeError(msg)
_run(connection)
