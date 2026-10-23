from datetime import timedelta

import sqlalchemy as sa

from obox_builder.conf import settings
from obox_builder.contrib.imports.models import ImportSession
from obox_builder.core.management.base import command
from obox_builder.core.utils.time import utcnow
from obox_builder.db.session import session_scope


@command(help="Delete expired spreadsheet imports and their staged data.")
async def handle() -> None:
    async with session_scope() as db:
        await db.execute(
            sa.delete(ImportSession).where(
                ImportSession.created_at < utcnow() - timedelta(days=settings.IMPORT_RETENTION_DAYS)
            )
        )
