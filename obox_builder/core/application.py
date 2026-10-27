from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager
from importlib import import_module
from typing import Any, cast
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from obox_builder import __version__
from obox_builder.conf import settings
from obox_builder.contrib.i18n.urls import I18nPatterns
from obox_builder.core.exceptions import ImproperlyConfigured, NotAuthenticated, PermissionDenied
from obox_builder.utils import import_string

logger = logging.getLogger(__name__)


def _wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept and "application/json" not in accept


def _next_url(request: Request) -> str:
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return quote(target, safe="/")


async def not_authenticated_handler(request: Request, exc: Exception) -> Response:
    """HTML clients get a redirect to LOGIN_URL; JSON clients get 401."""
    if _wants_html(request):
        login_url = f"{settings.LOGIN_URL}?next={_next_url(request)}"
        response: Response
        if request.headers.get("hx-request") == "true":
            response = Response(status_code=401, headers={"HX-Redirect": login_url})
        else:
            response = RedirectResponse(login_url, status_code=303)
    else:
        response = JSONResponse({"detail": str(exc) or "Authentication required."}, status_code=401)
    response.delete_cookie(
        settings.SESSION_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=settings.SESSION_COOKIE_SECURE and not settings.DEBUG,
        path="/",
    )
    return response


async def permission_denied_handler(request: Request, exc: Exception) -> Response:
    if _wants_html(request):
        try:
            from jinja2 import TemplateNotFound

            from obox_builder.templating import render

            try:
                admin_site = getattr(request.state, "admin_site", None)
                site_header = getattr(admin_site, "site_header", "obox admin")
                return render(
                    request,
                    "admin/errors/403.html",
                    {
                        "site_name": site_header,
                        "site_header": site_header,
                        "site_title": getattr(admin_site, "site_title", site_header),
                        "reason": str(exc),
                    },
                    status_code=403,
                )
            except TemplateNotFound:
                pass
        except ImportError:
            pass
        return HTMLResponse(
            "<!doctype html><html><head><title>403 Forbidden</title></head>"
            "<body><h1>403 Forbidden</h1></body></html>",
            status_code=403,
        )
    return JSONResponse({"detail": str(exc) or "Permission denied."}, status_code=403)


async def integrity_error_handler(request: Request, exc: Exception) -> Response:
    """Log database details while returning a generic server error to clients."""
    if not isinstance(exc, IntegrityError):
        raise exc
    if settings.DEBUG:
        raise exc
    logger.error(
        "Database rejected a write during %s %s.",
        request.method,
        request.url.path,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return HTMLResponse(
        "<!doctype html><html><head><title>Server Error (500)</title></head>"
        "<body><h1>Server Error (500)</h1></body></html>",
        status_code=500,
    )


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Build the engine/sessionmaker at startup and dispose on shutdown."""
    from obox_builder import db
    from obox_builder.apps import apps

    application.state.sessionmaker = db.get_sessionmaker()
    try:
        async with AsyncExitStack() as stack:
            if apps.ready:
                for config in apps.get_app_configs():
                    await stack.enter_async_context(config.lifespan())
            yield
    finally:
        await db.dispose()


def _iter_routers(root_urlconf: str) -> Iterator[tuple[APIRouter, str] | I18nPatterns]:
    if not root_urlconf:
        return
    try:
        module = import_module(root_urlconf)
    except ImportError as exc:
        raise ImproperlyConfigured(
            f"Could not import ROOT_URLCONF {root_urlconf!r}: {exc}"
        ) from exc
    entries = getattr(module, "routers", None)
    if entries is None:
        raise ImproperlyConfigured(f"ROOT_URLCONF {root_urlconf!r} must define a 'routers' list.")
    for entry in entries:
        if isinstance(entry, I18nPatterns):
            yield entry
        elif isinstance(entry, APIRouter):
            yield entry, ""
        elif (
            isinstance(entry, tuple)
            and len(entry) == 2
            and isinstance(entry[0], APIRouter)
            and isinstance(entry[1], str)
        ):
            yield entry[0], entry[1]
        else:
            raise ImproperlyConfigured(
                f"ROOT_URLCONF {root_urlconf!r} entries must be APIRouter instances "
                "(APIRouter, prefix) tuples, or i18n_patterns() results."
            )


def _mount_static(application: FastAPI) -> None:
    """Mount static files: sources in DEBUG, the collected STATIC_ROOT otherwise."""
    from obox_builder.apps import apps
    from obox_builder.staticfiles import AppStaticFiles, served_static_directories

    if not apps.apps_ready:
        return

    directories = served_static_directories()
    if not directories:
        return
    static_url = settings.STATIC_URL.rstrip("/") or "/static"
    application.mount(static_url, AppStaticFiles(directories), name="static")


def create_app() -> FastAPI:
    """Build the FastAPI application from settings (routers and middleware)."""
    application = FastAPI(title="obox-builder", version=__version__, lifespan=lifespan)
    config = settings.load()
    application.add_exception_handler(NotAuthenticated, not_authenticated_handler)
    application.add_exception_handler(PermissionDenied, permission_denied_handler)
    application.add_exception_handler(IntegrityError, integrity_error_handler)
    if config.SECURE_SSL_REDIRECT:
        application.add_middleware(HTTPSRedirectMiddleware)
    for middleware_path in config.MIDDLEWARE:
        middleware = import_string(middleware_path)
        if not inspect.isclass(middleware):
            raise ImproperlyConfigured(
                f"MIDDLEWARE entry {middleware_path!r} must point to a class."
            )
        application.add_middleware(cast("Any", middleware))
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=config.ALLOWED_HOSTS,
        www_redirect=False,
    )
    prefix_default_language: bool | None = None
    for router_entry in _iter_routers(config.ROOT_URLCONF):
        if isinstance(router_entry, I18nPatterns):
            if (
                prefix_default_language is not None
                and prefix_default_language != router_entry.prefix_default_language
            ):
                raise ImproperlyConfigured(
                    "All i18n_patterns() groups in a root URLconf must use the same "
                    "prefix_default_language value."
                )
            prefix_default_language = router_entry.prefix_default_language
            application.state.i18n_patterns_used = True
            application.state.i18n_prefix_default_language = prefix_default_language
            for router, prefix in router_entry.iter_router_prefixes(
                config.LANGUAGES, config.LANGUAGE_CODE
            ):
                application.include_router(router, prefix=prefix)
        else:
            router, prefix = router_entry
            application.include_router(router, prefix=prefix)
    _mount_static(application)
    return application


def get_asgi_application() -> FastAPI:
    """Entry point for ASGI servers (``uvicorn config.asgi:application``)."""
    import obox_builder

    obox_builder.setup()
    return create_app()
