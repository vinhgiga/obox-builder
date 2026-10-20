from __future__ import annotations

import pytest
from obox_builder.contrib.admin.checks import check_model_admins
from obox_builder.contrib.comments.checks import check_comments_models
from obox_builder.contrib.comments.options import DISABLED, parse_comments
from obox_builder.core.exceptions import ImproperlyConfigured
from tests.support.apps.commentsdemo.models import Article, InvalidOptions, Ticket


def test_parse_true_uses_defaults() -> None:
    options = parse_comments(True)
    assert options.enabled
    assert options.max_length is None
    assert options.allow_replies is True
    assert options.edit_window_seconds is None


def test_parse_mapping() -> None:
    options = parse_comments({"max_length": 100, "allow_replies": False, "edit_window_seconds": 0})
    assert options.max_length == 100
    assert options.allow_replies is False
    assert options.edit_window_seconds == 0


def test_disabled_declarations() -> None:
    assert parse_comments(None) == DISABLED
    assert parse_comments(False) == DISABLED


@pytest.mark.parametrize(
    "raw",
    [
        "yes",
        1,
        {"unknown": True},
        {"max_length": 0},
        {"max_length": 20_001},
        {"max_length": True},
        {"edit_window_seconds": -1},
        {"allow_replies": "yes"},
    ],
)
def test_invalid_declarations_raise(raw: object) -> None:
    with pytest.raises(ImproperlyConfigured):
        parse_comments(raw)


def test_model_options_are_read_from_the_declaration() -> None:
    from obox_builder.contrib.comments.registry import get_options

    assert get_options(Ticket).edit_window_seconds == 3600
    assert get_options(Article).allow_replies is False


def test_checks_report_composite_pk_and_invalid_declaration(comments_env: None) -> None:
    ids = {message.id for message in check_comments_models()}
    assert "obox.comments.E301" in ids
    assert "obox.comments.E302" in ids


def test_admin_checks_report_commentable_models_without_ui(comments_env: None) -> None:
    messages = [message for message in check_model_admins() if message.id == "obox.comments.W301"]
    text = " ".join(str(message) for message in messages)
    assert "Legacy" in text
    assert "Composite" in text
    assert "Ticket" not in text
    assert "Article" not in text
    assert "InvalidOptions" not in text


def test_invalid_declaration_model_is_not_reported_as_missing_ui(comments_env: None) -> None:
    assert InvalidOptions.__name__ not in " ".join(str(message) for message in check_model_admins())
