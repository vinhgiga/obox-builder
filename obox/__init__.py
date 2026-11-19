"""Model-driven forms, views and APIs for FastAPI and SQLAlchemy."""

from obox.application import Obox as Obox

__version__ = "0.1.0"


def setup() -> None:
    from obox.apps import apps
    from obox.conf import settings

    apps.populate(settings.INSTALLED_APPS)


__all__ = ["Obox", "setup"]
