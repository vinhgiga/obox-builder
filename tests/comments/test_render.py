from __future__ import annotations

from markupsafe import Markup
from obox_builder.contrib.comments.render import render_body


def test_plain_text_is_escaped_and_newlines_become_breaks() -> None:
    rendered = render_body("<b>hi</b>\nsecond & line")
    assert isinstance(rendered, Markup)
    assert "&lt;b&gt;hi&lt;/b&gt;" in rendered
    assert "second &amp; line" in rendered
    assert "<br>" in rendered
    assert "<b>" not in rendered


def test_http_https_and_mailto_links_are_allowed() -> None:
    rendered = str(render_body("see https://example.com/a?b=1, http://x.test, mailto:a@b.test."))
    assert '<a href="https://example.com/a?b=1"' in rendered
    assert 'rel="noopener noreferrer nofollow ugc"' in rendered
    assert '<a href="http://x.test"' in rendered
    assert '<a href="mailto:a@b.test"' in rendered
    assert "1</a>," in rendered


def test_other_schemes_are_not_linked() -> None:
    rendered = str(render_body("javascript:alert(1) and ftp://example.com"))
    assert "<a" not in rendered
    assert "javascript:alert(1)" in rendered


def test_known_mention_uses_current_display_name() -> None:
    rendered = str(render_body("hello @[u:7]!", {7: "Ann Lee"}))
    assert '<span class="comment-mention">@Ann Lee</span>' in rendered
    assert "@[u:7]" not in rendered


def test_unknown_mention_stays_neutral() -> None:
    rendered = str(render_body("hello @[u:7]", {}))
    assert '<span class="comment-mention">@unknown</span>' in rendered
    assert "7" not in rendered


def test_mention_names_are_escaped() -> None:
    rendered = str(render_body("@[u:7]", {7: "<script>alert(1)</script>"}))
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_markup_in_urls_is_escaped() -> None:
    rendered = str(render_body('https://example.com/"><script>alert(1)</script>'))
    assert "<script>" not in rendered
    assert "&#34;&gt;&lt;script&gt;" in rendered


def test_empty_body_renders_empty_markup() -> None:
    assert str(render_body("")) == ""
    assert str(render_body(None)) == ""
