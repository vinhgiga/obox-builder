"""Admin registrations for the comments test app (loaded by autodiscovery)."""

from __future__ import annotations

from obox_builder.contrib.admin import ModelView, register

from .resources import ArticleResource, TicketResource


@register(TicketResource)
class TicketAdmin(ModelView):
    list_display = ("id", "title")
    search_fields = ("title",)
    ordering = ("-id",)


@register(ArticleResource)
class ArticleAdmin(ModelView):
    list_display = ("id", "title")
    ordering = ("-id",)
