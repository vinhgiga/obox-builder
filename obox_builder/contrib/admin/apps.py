from obox_builder.apps import AppConfig, apps
from obox_builder.conf import settings


class AdminConfig(AppConfig):
    label = "admin"
    verbose_name = "Administration"

    def ready(self) -> None:
        from obox_builder.contrib.admin import checks  # noqa: F401  (register checks)
        from obox_builder.contrib.admin.site import site

        site.autodiscover()
        if not apps.is_installed("obox_builder.contrib.history"):
            return
        from obox_builder.contrib.admin.navigation import NavGroup, NavItem
        from obox_builder.contrib.history import HISTORY_VIEW_PERMISSION, track

        if not any(
            item.url == "/admin/history" for group in site.navigation for item in group.items
        ):
            site.navigation.append(
                NavGroup(
                    label="Audit",
                    items=[
                        NavItem(
                            "History",
                            "/admin/history",
                            icon="ti-history",
                            required_perm=HISTORY_VIEW_PERMISSION,
                        )
                    ],
                )
            )
        if settings.HISTORY_TRACK_ALL_ADMIN_MODELS:
            for admin in site.get_registry().values():
                track(admin.model)
