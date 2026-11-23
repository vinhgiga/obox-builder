"""Public file feature and attachment declarations."""

from obox.contrib.files.feature import Files
from obox.contrib.files.fields import attachments
from obox.contrib.files.models import File
from obox.contrib.files.rules import IMAGES, VIDEOS

__all__ = ["IMAGES", "VIDEOS", "File", "Files", "attachments"]
