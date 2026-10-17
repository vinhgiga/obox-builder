from obox_builder.apps import AppConfig


class AdminConfig(AppConfig):
    label = "admin"
    verbose_name = "Administration"

    def ready(self) -> None:
        from obox_builder.contrib.admin import checks  # noqa: F401  (register checks)
        from obox_builder.contrib.admin.site import site

        site.autodiscover()
