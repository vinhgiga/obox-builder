"""File identifiers remain writable while file metadata stays private."""

from typing import Any

from obox.rest_framework.fields import JSONField


class AttachmentField(JSONField):
    def to_representation(self, value: Any) -> list[str]:
        return [str(file.id) for file in value]
