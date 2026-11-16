"""Optional files application."""

from obox.apps import AppConfig


class FilesConfig(AppConfig):
    name = "obox.contrib.files"

    def ready(self) -> None:
        from obox.conf import settings
        from obox.contrib.files import Files

        self._service = Files(
            **{"root": settings.MEDIA_ROOT, **dict(getattr(settings, "FILES", {}))}
        )
