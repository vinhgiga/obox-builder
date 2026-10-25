from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_custom_auth_user_is_loaded_before_dependent_models() -> None:
    project_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["OBOX_SETTINGS_MODULE"] = "tests.support.custom_auth_settings"
    script = """
from sqlalchemy.orm import configure_mappers
from obox_builder import setup
setup()

from obox_builder.apps import apps
from obox_builder.contrib.auth.models import USER_VIEW_PERMISSION, User
from obox_builder.contrib.admin.navigation import default_navigation
from obox_builder.db.base import ModelBase

custom_user = apps.get_model("customusers", "Account")
assert User is custom_user
assert User.__tablename__ == "customusers_account"
assert USER_VIEW_PERMISSION == "customusers.view_account"
user_item = next(
    item for group in default_navigation() for item in group.items if item.url == "/admin/users"
)
assert user_item.required_perm == USER_VIEW_PERMISSION
configure_mappers()
for table_name in (
    "auth_session",
    "auth_user_roles",
    "admin_saved_view",
    "comments_row_comment",
    "comments_comment_mention",
):
    table = ModelBase.metadata.tables[table_name]
    assert any(
        fk.target_fullname == "customusers_account.id"
        for column in table.columns
        for fk in column.foreign_keys
    ), table_name
"""
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        check=False,
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
