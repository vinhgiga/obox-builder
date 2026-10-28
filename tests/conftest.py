from __future__ import annotations

import sys
from collections.abc import Iterator

import obox_builder
import pytest
from obox_builder import conf
from obox_builder.apps import apps
from obox_builder.apps.registry import Apps
from obox_builder.contrib.auth.authz import authorization_catalog
from obox_builder.contrib.history import context as history_context
from obox_builder.contrib.history import tracker as history_tracker
from obox_builder.core.checks import registry as checks_registry
from obox_builder.core.management import utils as cli_utils
from obox_builder.core.signals import post_migrate
from obox_builder.db import engine as db_engine

from tests.support import events

SUPPORT_APPS_PREFIX = "tests.support.apps"
MODEL_APP_MODULES = {
    "tests.support.apps.shop",
    "tests.support.apps.warehouse",
    "tests.support.apps.commentsdemo",
}


def _purge_support_app_modules() -> None:
    keep = MODEL_APP_MODULES
    for name in list(sys.modules):
        if any(name == module or name.startswith(f"{module}.") for module in keep):
            continue
        if name == SUPPORT_APPS_PREFIX or name.startswith(f"{SUPPORT_APPS_PREFIX}."):
            del sys.modules[name]


@pytest.fixture(autouse=True)
def guard_global_state() -> Iterator[None]:
    saved_permission_catalog = dict(authorization_catalog.permissions)
    saved_role_catalog = dict(authorization_catalog.roles)
    saved_settings = conf.settings._wrapped
    saved_apps = Apps()
    saved_apps.app_configs = dict(apps.app_configs)
    saved_apps.all_models = {label: dict(models) for label, models in apps.all_models.items()}
    saved_apps.all_tables = {label: dict(tables) for label, tables in apps.all_tables.items()}
    saved_apps.apps_ready = apps.apps_ready
    saved_apps.models_ready = apps.models_ready
    saved_apps.ready = apps.ready
    saved_setup_done = obox_builder._setup_done
    saved_checks = list(checks_registry._checks)
    saved_cli_state = (cli_utils.state.verbosity, cli_utils.state.no_color)
    saved_post_migrate_receivers = dict(post_migrate._receivers)
    saved_trackers = dict(history_tracker._trackers)
    saved_explicit_trackers = dict(history_tracker._explicit)
    saved_tracker_built = history_tracker._built

    events.RECORDS.clear()
    history_context._reset()
    _purge_support_app_modules()
    try:
        yield
    finally:
        authorization_catalog.permissions = saved_permission_catalog
        authorization_catalog.roles = saved_role_catalog
        conf.settings._wrapped = saved_settings
        apps.app_configs = saved_apps.app_configs
        apps.all_models = saved_apps.all_models
        apps.all_tables = saved_apps.all_tables
        apps.apps_ready = saved_apps.apps_ready
        apps.models_ready = saved_apps.models_ready
        apps.ready = saved_apps.ready
        obox_builder._setup_done = saved_setup_done
        checks_registry._checks[:] = saved_checks
        cli_utils.state.verbosity, cli_utils.state.no_color = saved_cli_state
        post_migrate._receivers.clear()
        post_migrate._receivers.update(saved_post_migrate_receivers)
        history_tracker._trackers.clear()
        history_tracker._trackers.update(saved_trackers)
        history_tracker._explicit.clear()
        history_tracker._explicit.update(saved_explicit_trackers)
        history_tracker._built = saved_tracker_built
        history_context._reset()
        db_engine._reset()
        events.RECORDS.clear()
        _purge_support_app_modules()
