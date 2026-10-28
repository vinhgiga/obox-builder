from __future__ import annotations

from datetime import datetime

import pytest
from jinja2 import TemplateNotFound
from obox_builder.templating import build_context, get_environment, render, render_template
from starlette.requests import Request


def test_renders_template_from_installed_app(env: None) -> None:
    html = render_template(
        "shop/product_list.html",
        {"products": [{"name": "Widget"}, {"name": "Gadget"}]},
    )
    assert "<li>Widget</li>" in html
    assert "<li>Gadget</li>" in html


def test_template_dirs_win_over_app_templates(env: None) -> None:
    html = render_template("greeting.html", {"name": "World"})
    assert "<p>Hello World</p>" in html


def test_autoescape_is_on(env: None) -> None:
    html = render_template("greeting.html", {"name": "<b>bold</b>"})
    assert "<p>Hello &lt;b&gt;bold&lt;/b&gt;</p>" in html
    assert "<p>Raw: <b>bold</b></p>" in html


def test_missing_template_raises(env: None) -> None:
    with pytest.raises(TemplateNotFound):
        render_template("does/not/exist.html", {})


def test_environment_is_reused(env: None) -> None:
    assert get_environment() is get_environment()


def test_request_context_processor(env: None, http_request: Request) -> None:
    context = build_context(http_request)
    assert context["request"] is http_request


def test_auth_context_processor_when_installed(env: None, http_request: Request) -> None:
    context = build_context(http_request)
    assert context["user"].username == ""
    assert not context["perms"].shop.product.read


def test_explicit_context_wins(env: None, http_request: Request) -> None:
    context = build_context(http_request, {"user": "overridden"})
    assert context["user"] == "overridden"


def test_render_returns_html_response(env: None, http_request: Request) -> None:
    response = render(http_request, "greeting.html", {"name": "HTTP"})
    assert response.status_code == 200
    assert response.media_type == "text/html"
    assert b"<p>Hello HTTP</p>" in response.body


def test_static_global(env: None) -> None:
    environment = get_environment()
    static = environment.globals["static"]
    assert callable(static)
    assert static("admin/css/admin.css") == "/static/admin/css/admin.css"


def test_filters(env: None) -> None:
    environment = get_environment()
    assert environment.filters["format_datetime"](datetime(2026, 1, 2, 3, 4)) == "2026-01-02 03:04"
    assert environment.filters["filesizeformat"](1536) == "1.5 KB"
    querystring = environment.globals["querystring"]
    assert callable(querystring)
    assert querystring({"page": 2, "q": "x"}, q="y", empty=None) == "page=2&q=y"
