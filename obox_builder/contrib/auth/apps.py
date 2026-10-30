"""Auth app config: wires permission sync to the ``post_migrate`` signal."""

from __future__ import annotations

from obox_builder.apps.config import AppConfig


class AuthConfig(AppConfig):
    name = "obox_builder.contrib.auth"
    label = "auth"
    verbose_name = "Authentication"

    def finalize(self) -> None:
        from obox_builder.contrib.auth.permissions import initialize_catalog

        initialize_catalog()

    def ready(self) -> None:
        from obox_builder.contrib.auth.permissions import sync_permissions_receiver
        from obox_builder.core.signals import post_migrate

        post_migrate.connect(
            sync_permissions_receiver,
            dispatch_uid="obox_builder.contrib.auth.sync_permissions",
        )
