from obox_builder.apps import AppConfig, apps
from obox_builder.conf import settings


class AdminConfig(AppConfig):
    label = "admin"
    verbose_name = "Administration"

    def ready(self) -> None:
        from obox_builder.contrib.admin import checks  # noqa: F401  (register checks)
        from obox_builder.contrib.admin.site import site

        site.autodiscover()
        if settings.HISTORY_TRACK_ALL_ADMIN_MODELS and apps.is_installed(
            "obox_builder.contrib.history"
        ):
            from obox_builder.contrib.history import track

            for admin in site.get_registry().values():
                track(admin.model)
