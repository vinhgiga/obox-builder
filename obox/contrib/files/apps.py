"""Optional files application."""

from typing import Any

from obox.contrib._apps import FeatureConfig


class FilesConfig(FeatureConfig):
    name = "obox.contrib.files"
    feature = "Files"
    setting = "FILES"

    def get_options(self, settings: Any) -> dict[str, Any]:
        return {"root": settings.MEDIA_ROOT, **super().get_options(settings)}
