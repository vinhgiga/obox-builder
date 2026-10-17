from __future__ import annotations

import os

import pytest
from obox_builder.conf import SETTINGS_MODULE_ENV

# Model classes require a ready app registry at import time (Django-style), and
# test modules import them at module level (see tests/auth/conftest.py).
os.environ.setdefault(SETTINGS_MODULE_ENV, "tests.support.admin_settings")

import obox_builder as _obox_builder  # noqa: E402
from obox_builder.conf import settings as _settings  # noqa: E402
from obox_builder.core.signals import post_migrate as _post_migrate  # noqa: E402

_obox_builder.setup()
_settings.reset()
_obox_builder._reset()
_post_migrate.clear()


@pytest.fixture
async def db_ready() -> None:
    """Create tables + sync permissions, i.e. the state a migrated project is in."""
    import obox_builder

    obox_builder.setup()
    from obox_builder.contrib.auth.permissions import sync_permissions
    from obox_builder.db import get_engine
    from obox_builder.db.base import ModelBase
    from obox_builder.db.session import session_scope

    async with get_engine().begin() as connection:
        await connection.run_sync(ModelBase.metadata.drop_all)
        await connection.run_sync(ModelBase.metadata.create_all)
    async with session_scope() as db:
        await sync_permissions(db)
    return
