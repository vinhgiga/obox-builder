from __future__ import annotations

import os

from obox_builder.conf import SETTINGS_MODULE_ENV

# Model classes require a ready app registry at import time, so the shop app
# must be populated before test modules import its models.
os.environ.setdefault(SETTINGS_MODULE_ENV, "tests.support.admin_settings")

import obox_builder  # noqa: E402
from obox_builder.conf import settings as _settings  # noqa: E402
from obox_builder.core.signals import post_migrate  # noqa: E402

obox_builder.setup()
_settings.reset()
obox_builder._reset()
post_migrate.clear()
