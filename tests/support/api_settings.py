from tests.support.history_settings import Settings as HistorySettings


class Settings(HistorySettings):
    INSTALLED_APPS: list[str] = [
        "obox_builder.contrib.history",
        "obox_builder.contrib.comments",
        "obox_builder.contrib.auth",
        "obox_builder.contrib.admin",
        "obox_builder.contrib.savedviews",
        "tests.support.apps.shop",
        "tests.support.apps.warehouse",
    ]
