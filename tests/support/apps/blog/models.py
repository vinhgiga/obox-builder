from __future__ import annotations

from obox_builder.apps import apps

from tests.support import events

events.RECORDS.append("blog.models")


class Post:
    pass


apps.register_model("blog", Post)
