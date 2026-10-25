from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from types import ModuleType

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Request
from obox_builder.apps import apps
from obox_builder.conf import BaseSettings, settings
from obox_builder.contrib.i18n import (
    clear_translation_cache,
    get_language,
    gettext,
    ngettext,
    override,
)
from obox_builder.contrib.i18n.middleware import LocaleMiddleware, select_language
from obox_builder.contrib.i18n.urls import get_language_from_path, i18n_patterns
from obox_builder.core.application import create_app
from obox_builder.fields import FieldSpec, field_types
from obox_builder.http.i18n import router as i18n_router
from obox_builder.templating import get_environment
from pydantic import SecretStr


@pytest.fixture
def i18n_settings() -> None:
    settings.configure(
        BaseSettings(
            SECRET_KEY=SecretStr("s" * 40),
            DATABASE_URL="sqlite+aiosqlite://",
            DEBUG=True,
        )
    )
    clear_translation_cache()


@pytest.mark.parametrize(
    ("cookie", "header", "default", "expected"),
    [
        ("vi", "en-US,en;q=0.9", "en", "vi"),
        ("fr", "fr-CA,vi;q=0.8,en;q=0.7", "en", "vi"),
        (None, "en;q=0, *;q=1", "en", "vi"),
        (None, "vi;q=0, en-US;q=0.5", "vi", "en"),
        (None, "fr-CA,fr;q=0.9", "vi", "vi"),
        (None, None, "vi", "vi"),
    ],
)
def test_select_language(
    i18n_settings: None,
    cookie: str | None,
    header: str | None,
    default: str,
    expected: str,
) -> None:
    assert select_language(cookie, header, default) == expected


def test_language_variants_and_configured_choices() -> None:
    languages = (("en", "English"), ("fr-ca", "French (Canada)"))
    assert select_language(None, "fr-FR,fr;q=0.8", "en", languages=languages) == "fr-ca"
    assert select_language(None, "vi,en;q=0.5", "en", languages=languages) == "en"
    assert get_language_from_path("/fr-CA/articles", languages=languages) == "fr-ca"
    assert get_language_from_path("/vi/articles", languages=languages) is None


@pytest.mark.parametrize("prefix_default_language", [True, False])
async def test_i18n_patterns_prefix_precedes_cookie_and_header(
    i18n_settings: None,
    monkeypatch: pytest.MonkeyPatch,
    prefix_default_language: bool,
) -> None:
    router = APIRouter()

    @router.get("/locale")
    async def locale(request: Request) -> dict[str, str]:
        return {"language": request.state.language_code}

    urlconf = ModuleType("test_i18n_urlconf")
    urlconf.__dict__["routers"] = [
        i18n_patterns(router, prefix_default_language=prefix_default_language)
    ]
    monkeypatch.setitem(sys.modules, urlconf.__name__, urlconf)
    settings.configure(
        BaseSettings(
            SECRET_KEY=SecretStr("s" * 40),
            DATABASE_URL="sqlite+aiosqlite://",
            DEBUG=True,
            ROOT_URLCONF=urlconf.__name__,
        )
    )
    app = create_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        vietnamese = await client.get(
            "/vi/locale", headers={"Cookie": "obox_language=en", "Accept-Language": "en"}
        )
        if prefix_default_language:
            english = await client.get("/en/locale", headers={"Cookie": "obox_language=vi"})
        else:
            english = await client.get(
                "/locale", headers={"Cookie": "obox_language=vi", "Accept-Language": "vi"}
            )

    assert vietnamese.json() == {"language": "vi"}
    assert english.json() == {"language": "en"}


async def test_i18n_patterns_redirect_to_a_matching_language_url(
    i18n_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = APIRouter()

    @router.get("/locale")
    async def locale(request: Request) -> dict[str, str]:
        return {"language": request.state.language_code}

    urlconf = ModuleType("test_i18n_redirect_urlconf")
    urlconf.__dict__["routers"] = [i18n_patterns(router)]
    monkeypatch.setitem(sys.modules, urlconf.__name__, urlconf)
    settings.configure(
        BaseSettings(
            SECRET_KEY=SecretStr("s" * 40),
            DATABASE_URL="sqlite+aiosqlite://",
            DEBUG=True,
            ROOT_URLCONF=urlconf.__name__,
        )
    )
    app = create_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        preferred = await client.get(
            "/locale/?page=2",
            headers={"Cookie": "obox_language=vi", "Accept-Language": "en"},
            follow_redirects=False,
        )
        localized = await client.get(
            preferred.headers["location"], headers={"Cookie": "obox_language=en"}
        )
        defaulted = await client.get("/locale", headers={"Accept-Language": "en"})
        missing = await client.get("/unknown", headers={"Cookie": "obox_language=vi"})

    assert preferred.status_code == 302
    assert preferred.headers["location"] == "/vi/locale?page=2"
    assert preferred.headers["content-language"] == "vi"
    assert set(preferred.headers["vary"].split(", ")) == {"Accept-Language", "Cookie"}
    assert localized.json() == {"language": "vi"}
    assert defaulted.headers["location"] == "/en/locale"
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_locale_middleware_is_request_scoped_and_sets_response_headers(
    i18n_settings: None,
) -> None:
    app = FastAPI()
    app.add_middleware(LocaleMiddleware)

    @app.get("/locale")
    @app.get("/vi/locale")
    async def locale(request: Request) -> dict[str, str]:
        await asyncio.sleep(0)
        return {
            "state": request.state.language_code,
            "active": get_language(),
            "message": gettext("Log in"),
        }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        english, vietnamese = await asyncio.gather(
            client.get("/locale", headers={"Accept-Language": "en"}),
            client.get("/locale", headers={"Accept-Language": "vi-VN,vi;q=0.9"}),
        )
        prefixed_without_patterns = await client.get(
            "/vi/locale",
            headers={"Cookie": "obox_language=en", "Accept-Language": "vi"},
        )

    assert english.json() == {"state": "en", "active": "en", "message": "Log in"}
    assert vietnamese.json() == {"state": "vi", "active": "vi", "message": "Đăng nhập"}
    assert vietnamese.headers["content-language"] == "vi"
    assert prefixed_without_patterns.json() == {"state": "en", "active": "en", "message": "Log in"}
    assert {part.strip() for part in vietnamese.headers["vary"].split(",")} == {
        "Accept-Language",
        "Cookie",
    }


@pytest.mark.asyncio
async def test_set_language_persists_supported_choice_and_rejects_unsafe_requests(
    i18n_settings: None,
) -> None:
    app = FastAPI()
    app.add_middleware(LocaleMiddleware)
    app.include_router(i18n_router)

    @app.get("/language")
    async def language(request: Request) -> dict[str, str]:
        return {"language": request.state.language_code}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        smooth = await client.post(
            "/i18n/set-language",
            data={"language": "vi", "next": "/admin/"},
            headers={
                "HX-Request": "true",
                "HX-Target": "main#admin-main",
                "HX-Request-Type": "partial",
            },
            follow_redirects=False,
        )
        changed = await client.post(
            "/i18n/set-language",
            data={"language": "vi", "next": "/admin/login"},
            follow_redirects=False,
        )
        selected = await client.get("/language", headers={"Accept-Language": "en"})
        unsupported = await client.post(
            "/i18n/set-language", data={"language": "fr"}, follow_redirects=False
        )
        unsafe = await client.post(
            "/i18n/set-language",
            data={"language": "en"},
            headers={"Origin": "https://elsewhere.example"},
            follow_redirects=False,
        )
        unsafe_redirect = await client.post(
            "/i18n/set-language",
            data={"language": "en", "next": "https://elsewhere.example"},
            follow_redirects=False,
        )

    assert smooth.status_code == 200
    assert json.loads(smooth.headers["hx-location"]) == {
        "path": "/admin/",
        "target": "#admin-main",
        "swap": "outerHTML",
        "push": "/admin/",
    }
    assert "obox_language=vi" in smooth.headers["set-cookie"]
    assert changed.status_code == 303
    assert changed.headers["location"] == "/admin/login"
    assert "obox_language=vi" in changed.headers["set-cookie"]
    assert unsupported.status_code == 400
    assert unsafe.status_code == 403
    assert unsafe_redirect.headers["location"] == "/admin/"
    assert selected.json() == {"language": "vi"}


@pytest.mark.parametrize(
    ("prefix_default_language", "language", "next_url", "expected"),
    [
        (False, "vi", "/admin/users?q=test", "/vi/admin/users?q=test"),
        (False, "en", "/vi/admin/users", "/admin/users"),
        (True, "en", "/vi/admin/users", "/en/admin/users"),
    ],
)
async def test_set_language_updates_i18n_url_prefix(
    i18n_settings: None,
    prefix_default_language: bool,
    language: str,
    next_url: str,
    expected: str,
) -> None:
    app = FastAPI()
    app.state.i18n_patterns_used = True
    app.state.i18n_prefix_default_language = prefix_default_language
    app.include_router(i18n_router)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/i18n/set-language",
            data={"language": language, "next": next_url},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == expected


@pytest.mark.parametrize(
    ("header", "expected_header"),
    [("Location", "location"), ("HX-Redirect", "hx-redirect")],
)
async def test_localized_admin_redirect_keeps_url_language(
    i18n_settings: None, header: str, expected_header: str
) -> None:
    from starlette.responses import RedirectResponse, Response

    app = FastAPI()
    app.state.i18n_patterns_used = True
    app.state.i18n_prefix_default_language = False
    app.add_middleware(LocaleMiddleware)

    @app.get("/vi/redirect")
    async def redirect() -> Response:
        if header == "Location":
            return RedirectResponse("/admin/login")
        return Response(status_code=401, headers={header: "/admin/login?next=/admin/"})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/vi/redirect")

    assert response.headers[expected_header] == (
        "/vi/admin/login" if header == "Location" else "/vi/admin/login?next=/admin/"
    )


def test_gettext_and_jinja_use_the_active_locale(i18n_settings: None) -> None:
    apps.populate([])
    with override("vi"):
        assert get_language() == "vi"
        assert gettext("Log in") == "Đăng nhập"
        assert ngettext("%(num)d item", "%(num)d items", 2) == "%(num)d mục"
        rendered = (
            get_environment()
            .from_string(
                "{% trans %}Log in to your account{% endtrans %} "
                "{{ pgettext('navigation', 'Home') }} "
                "{{ ngettext('%(num)d item', '%(num)d items', 2) }}"
            )
            .render()
        )
        assert rendered == "Đăng nhập vào tài khoản của bạn Trang chủ 2 mục"
    assert get_language() == "en"


def test_field_display_formats_follow_the_active_locale(i18n_settings: None) -> None:
    date_field = FieldSpec(name="date", label="Date", type=field_types.create("date"))
    number_field = FieldSpec(name="amount", label="Amount", type=field_types.create("integer"))

    assert date_field.type.cell(date(2026, 10, 25), date_field).text == "2026-10-25"
    assert number_field.type.cell(1234, number_field).text == "1,234"
    with override("vi"):
        assert date_field.type.cell(date(2026, 10, 25), date_field).text == "25/10/2026"
        assert number_field.type.cell(1234, number_field).text == "1.234"
