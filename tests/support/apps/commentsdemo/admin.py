"""Admin registrations for the comments test app (loaded by autodiscovery)."""

from __future__ import annotations

from obox_builder.contrib.admin import ModelAdmin, register

from tests.support.apps.commentsdemo.models import Article, Ticket


@register(Ticket)
class TicketAdmin(ModelAdmin):
    list_display = ("id", "title")
    search_fields = ("title",)
    ordering = ("-id",)


@register(Article)
class ArticleAdmin(ModelAdmin):
    list_display = ("id", "title")
    ordering = ("-id",)
