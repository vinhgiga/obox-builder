from __future__ import annotations

from obox_builder.contrib.savedviews.resources import SavedViews
from obox_builder.contrib.comments.resources import Comments

from obox_builder.resources import ModelResource, register

from .models import Article, InvalidOptions, Legacy, Plain, Ticket


@register()
class TicketResource(Comments, SavedViews, ModelResource[Ticket]):
    name = "commentsdemo.ticket"
    model = Ticket
    fields = ("id", "title", "created_at", "updated_at")


@register()
class ArticleResource(Comments, SavedViews, ModelResource[Article]):
    name = "commentsdemo.article"
    model = Article
    fields = ("id", "title", "created_at", "updated_at")


@register()
class LegacyResource(Comments, SavedViews, ModelResource[Legacy]):
    name = "commentsdemo.legacy"
    model = Legacy
    fields = ("id", "title")


@register()
class PlainResource(SavedViews, ModelResource[Plain]):
    name = "commentsdemo.plain"
    model = Plain
    fields = ("id", "title")


@register()
class InvalidOptionsResource(SavedViews, ModelResource[InvalidOptions]):
    name = "commentsdemo.invalidOptions"
    model = InvalidOptions
    fields = ("id",)
