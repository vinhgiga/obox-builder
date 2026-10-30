from tests.support.auth_settings import Settings as AuthSettings


class Settings(AuthSettings):
    INSTALLED_APPS: list[str] = [
        "obox_builder.contrib.imports",
        "obox_builder.contrib.jobs",
        "obox_builder.contrib.events",
        "obox_builder.contrib.history",
        "obox_builder.contrib.auth",
        "obox_builder.contrib.admin",
        "obox_builder.contrib.savedviews",
        "tests.support.apps.shop",
        "tests.support.apps.warehouse",
    ]
