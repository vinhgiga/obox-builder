from __future__ import annotations

from obox_builder.apps import AppConfig, apps


class JobsConfig(AppConfig):
    name = "obox_builder.contrib.jobs"
    label = "jobs"
    verbose_name = "Background jobs"
    permissions = [
        ("retry_task", "Can retry background tasks"),
        ("revoke_task", "Can revoke background tasks"),
        ("view_task_payload", "Can view task arguments and results"),
    ]

    def ready(self) -> None:
        from obox_builder.utils.module_loading import autodiscover_modules

        autodiscover_modules("tasks", packages=[config.name for config in apps.get_app_configs()])
