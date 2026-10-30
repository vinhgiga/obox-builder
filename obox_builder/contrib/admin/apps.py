from obox_builder.apps import AppConfig, apps
from obox_builder.conf import settings


class AdminConfig(AppConfig):
    label = "admin"
    verbose_name = "Administration"

    def finalize(self) -> None:
        from obox_builder.contrib.admin.site import site

        site.finalize()

    def ready(self) -> None:
        from obox_builder.contrib.admin import checks  # noqa: F401  (register checks)
        from obox_builder.contrib.admin.navigation import default_navigation
        from obox_builder.contrib.admin.site import site

        site.navigation = default_navigation()
        site.autodiscover()
        from obox_builder.contrib.admin.token_admin import ApiTokenAdmin
        from obox_builder.contrib.auth.models import ApiToken
        from obox_builder.contrib.auth.resources import ApiTokenResource

        if site.get_model_admin_for(ApiToken) is None:
            site.register(ApiTokenResource, ApiTokenAdmin)
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
