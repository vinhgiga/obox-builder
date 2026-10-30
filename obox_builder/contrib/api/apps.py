from obox_builder.apps import AppConfig


class ApiConfig(AppConfig):
    name = "obox_builder.contrib.api"
    label = "api"
    verbose_name = "REST API"

    def ready(self) -> None:
        from obox_builder.apps import apps
        from obox_builder.contrib.api import checks  # noqa: F401
        from obox_builder.utils.module_loading import autodiscover_modules

        autodiscover_modules("api", packages=[config.name for config in apps.get_app_configs()])
