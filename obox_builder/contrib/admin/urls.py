"""Router for the admin site, mounted by the project ``ROOT_URLCONF`` at ``/admin``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request

from obox_builder.contrib.admin.deps import bind_history
from obox_builder.contrib.admin.views import (
    dashboard,
    login_submit,
    login_view,
    logout_view,
    password_change_submit,
    password_change_view,
    role_create,
    role_create_submit,
    role_delete,
    role_edit,
    role_edit_submit,
    role_history,
    role_list,
    user_create,
    user_create_submit,
    user_edit,
    user_edit_submit,
    user_history,
    user_list,
)
from obox_builder.contrib.admin.views.history import model_timeline_available
from obox_builder.contrib.admin.views.model import register_model_routes
from obox_builder.contrib.auth.models import Role, User

if TYPE_CHECKING:
    from obox_builder.contrib.admin.site import AdminSite


def build_router(site: AdminSite) -> APIRouter:
    async def bind_site(request: Request) -> None:
        request.state.admin_site = site

    router = APIRouter(tags=["admin"], dependencies=[Depends(bind_site), Depends(bind_history)])

    router.add_api_route("/", dashboard, methods=["GET"], name="admin:dashboard")
    router.add_api_route("/login", login_view, methods=["GET"], name="admin:login")
    router.add_api_route("/login", login_submit, methods=["POST"], name="admin:login_submit")
    router.add_api_route("/logout", logout_view, methods=["POST"], name="admin:logout")
    router.add_api_route(
        "/password_change", password_change_view, methods=["GET"], name="admin:password_change"
    )
    router.add_api_route(
        "/password_change",
        password_change_submit,
        methods=["POST"],
        name="admin:password_change_submit",
    )

    router.add_api_route("/users", user_list, methods=["GET"], name="admin:user_list")
    router.add_api_route("/users/new", user_create, methods=["GET"], name="admin:user_create")
    router.add_api_route(
        "/users/new", user_create_submit, methods=["POST"], name="admin:user_create_submit"
    )
    router.add_api_route("/users/{user_id}", user_edit, methods=["GET"], name="admin:user_edit")
    router.add_api_route(
        "/users/{user_id}", user_edit_submit, methods=["POST"], name="admin:user_edit_submit"
    )
    if model_timeline_available(User):
        router.add_api_route(
            "/users/{user_id}/history",
            user_history,
            methods=["GET"],
            name="admin:user_history",
        )

    router.add_api_route("/roles", role_list, methods=["GET"], name="admin:role_list")
    router.add_api_route("/roles/new", role_create, methods=["GET"], name="admin:role_create")
    router.add_api_route(
        "/roles/new", role_create_submit, methods=["POST"], name="admin:role_create_submit"
    )
    router.add_api_route("/roles/{role_id}", role_edit, methods=["GET"], name="admin:role_edit")
    router.add_api_route(
        "/roles/{role_id}", role_edit_submit, methods=["POST"], name="admin:role_edit_submit"
    )
    router.add_api_route(
        "/roles/{role_id}/delete", role_delete, methods=["POST"], name="admin:role_delete"
    )
    if model_timeline_available(Role):
        router.add_api_route(
            "/roles/{role_id}/history",
            role_history,
            methods=["GET"],
            name="admin:role_history",
        )
    register_model_routes(router, site)
    return router
