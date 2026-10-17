from __future__ import annotations

import os
import subprocess

import sqlalchemy as sa

from obox_builder.conf import settings
from obox_builder.core.management.base import CommandError, command


@command(help="Open the database shell (psql or sqlite3).", requires_checks=True)
def handle() -> None:
    from obox_builder.db import to_sync_url

    url = sa.engine.make_url(to_sync_url(settings.DATABASE_URL))
    if url.drivername.startswith("postgresql"):
        args = ["psql"]
        if url.host:
            args += ["-h", url.host]
        if url.port:
            args += ["-p", str(url.port)]
        if url.username:
            args += ["-U", url.username]
        if url.database:
            args += ["-d", url.database]
        env = dict(os.environ)
        if url.password:
            env["PGPASSWORD"] = url.password
        _run(args, env)
        return
    if url.drivername == "sqlite":
        if not url.database:
            raise CommandError("DATABASE_URL has no database file for sqlite.")
        _run(["sqlite3", url.database], None)
        return
    raise CommandError(f"dbshell does not support driver {url.drivername!r}.")


def _run(args: list[str], env: dict[str, str] | None) -> None:
    try:
        subprocess.run(args, env=env, check=False)  # noqa: S603, S607
    except FileNotFoundError:
        raise CommandError(f"{args[0]!r} was not found on PATH.") from None
