from __future__ import annotations

import another_missing_module_xyz  # noqa: F401
from obox_builder.apps import AppConfig


class BadConfig(AppConfig):
    pass
