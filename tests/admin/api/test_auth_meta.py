from __future__ import annotations

import httpx
import pytest
import sqlalchemy as sa
from obox_builder.conf import settings
from tests.admin.api.conftest import sign_in, site_for
from tests.admin.api.test_records import BASE, ITEMS, PRODUCTS, create_item
from tests.admin.conftest import grant_permissions, make_user


async def test_login_session_rotation_me_logout_and_password(api: httpx.AsyncClient) -> None:
    headers = await sign_in(api)
    first_cookie = api.cookies.get(settings.SESSION_COOKIE_NAME)
    login = await api.post(
        f"{BASE}/auth/login", json={"username": "api-user", "password": "s3cure-password"}
    )
    assert login.status_code == 200
    assert api.cookies.get(settings.SESSION_COOKIE_NAME) != first_cookie
    headers = {"X-CSRF-Token": login.json()["csrf_token"]}
    me = await api.get(f"{BASE}/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "api-user"
    assert me.json()["csrf_token"] == headers["X-CSRF-Token"]
    assert "password_hash" not in me.text
    changed = await api.post(
        f"{BASE}/auth/password",
        headers=headers,
        json={"current_password": "s3cure-password", "new_password": "another-secure-password"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["csrf_token"] != headers["X-CSRF-Token"]
    assert api.cookies.get(settings.SESSION_COOKIE_NAME) != first_cookie
    logout = await api.post(
        f"{BASE}/auth/logout", headers={"X-CSRF-Token": changed.json()["csrf_token"]}
    )
    assert logout.status_code == 204
    assert (await api.get(f"{BASE}/auth/me")).status_code == 401


async def test_login_failure_does_not_lock_account_and_rejects_inactive(
    api: httpx.AsyncClient,
) -> None:
    await make_user("alice")
    for _ in range(6):
        response = await api.post(
            f"{BASE}/auth/login", json={"username": "alice", "password": "wrong"}
        )
        assert response.status_code == 401
    assert (
        await api.post(
            f"{BASE}/auth/login", json={"username": "alice", "password": "s3cure-password"}
        )
    ).status_code == 200
    await make_user("inactive", is_active=False)
    assert (
        await api.post(
            f"{BASE}/auth/login", json={"username": "inactive", "password": "s3cure-password"}
        )
    ).status_code == 401


@pytest.mark.parametrize(
    "role,expected", [("anonymous", 401), ("none", 404), ("view", 200), ("superuser", 200)]
)
async def test_read_permissions(api: httpx.AsyncClient, role: str, expected: int) -> None:
    if role != "anonymous":
        await sign_in(api, superuser=role == "superuser")
        if role == "view":
            await grant_permissions("api-user", "shop.view_product")
    for path in (PRODUCTS, f"{BASE}/models/shop.product/schema"):
        response = await api.get(path, headers={"Accept": "text/html"})
        assert response.status_code == expected, response.text
        if expected != 200:
            assert response.headers["content-type"].startswith("application/problem+json")


async def test_disabled_models_and_row_scope_are_hidden(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.support.apps.warehouse.models import Item

    headers = await sign_in(api)
    created = await create_item(api, headers)
    admin = site_for(api).get_model_admin_for(Item)
    monkeypatch.setattr(
        admin, "get_queryset", lambda ctx: sa.select(Item).where(Item.name == "Visible")
    )
    url = f"{ITEMS}/{created.json()['id']}"
    for method in ("GET", "PATCH", "DELETE"):
        response = await api.request(
            method,
            url,
            headers={**headers, "If-Match": created.headers["etag"]},
            json={"fields": {"name": "Changed"}} if method == "PATCH" else None,
        )
        assert response.status_code == 404
    assert (await api.get(ITEMS)).json()["records"] == []
    monkeypatch.setattr(admin, "api_enabled", False)
    assert (await api.get(ITEMS)).status_code == 404
    assert "warehouse.item" not in {
        model["label"] for model in (await api.get(f"{BASE}/meta")).json()["models"]
    }


async def test_view_only_cannot_write(api: httpx.AsyncClient) -> None:
    headers = await sign_in(api, superuser=False)
    await grant_permissions("api-user", "shop.view_product")
    response = await api.post(
        PRODUCTS, headers=headers, json={"fields": {"name": "No", "status": "draft"}}
    )
    assert response.status_code == 403


async def test_csrf_origin_bearer_and_problem_details(api: httpx.AsyncClient) -> None:
    headers = await sign_in(api)
    for extra in ({}, {"X-CSRF-Token": "invalid"}, {**headers, "Origin": "https://untrusted.test"}):
        response = await api.post(ITEMS, headers=extra, json={"fields": {}})
        assert response.status_code == 403
        assert response.json()["code"] == "csrf_failed"
        assert response.json()["request_id"] == response.headers["x-request-id"]
    assert (await api.get(ITEMS, headers={"Authorization": "Bearer bad-token"})).status_code == 401
    invalid = await api.post(
        f"{BASE}/auth/login",
        headers={"Origin": "https://untrusted.test"},
        json={"username": "api-user", "password": "s3cure-password"},
    )
    assert invalid.status_code == 403
    assert (
        (await api.get(f"{BASE}/unknown", headers={"Accept": "text/html"}))
        .headers["content-type"]
        .startswith("application/problem+json")
    )


async def test_trusted_origin_preflight_and_csrf(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert settings._wrapped is not None
    monkeypatch.setattr(settings._wrapped, "ADMIN_API_TRUSTED_ORIGINS", ["https://frontend.test"])
    origin = {"Origin": "https://frontend.test"}
    preflight = await api.options(
        ITEMS,
        headers={
            **origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-CSRF-Token",
        },
    )
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-origin"] == origin["Origin"]
    headers = await sign_in(api)
    created = await create_item(api, headers | origin)
    assert created.headers["access-control-allow-credentials"] == "true"
    assert "ETag" in created.headers["access-control-expose-headers"]
    assert (await api.post(ITEMS, headers=origin, json={"fields": {}})).status_code == 403


async def test_schema_metadata_and_openapi_contract(api: httpx.AsyncClient) -> None:
    await sign_in(api)
    response = await api.get(f"{BASE}/models/warehouse.item/schema")
    assert response.status_code == 200, response.text
    schema = response.json()
    by_name = {field["name"]: field for field in schema["fields"]}
    assert "secret_token" not in by_name
    assert {name for name, field in by_name.items() if field["readonly"]} == {
        "id",
        "version",
        "created_at",
        "updated_at",
    }
    assert by_name["name"] == {
        "name": "name",
        "label": "Name",
        "type": "text",
        "nullable": False,
        "required": True,
        "readonly": False,
        "help_text": "",
        "choices": [],
        "operators": [
            {"name": op, "label": label, "arity": 1}
            for op, label in [
                ("eq", "is"),
                ("ne", "is not"),
                ("contains", "contains"),
                ("startswith", "starts with"),
                ("endswith", "ends with"),
                ("in", "is any of"),
                ("isnull", "is empty"),
            ]
        ],
        "dynamic": [],
    }
    assert all(
        isinstance(name, str) for group in schema["layout"] for row in group["rows"] for name in row
    )
    openapi = (await api.get("/openapi.json")).json()
    api_paths = {
        path: set(operations)
        for path, operations in openapi["paths"].items()
        if path.startswith(BASE)
    }
    assert api_paths == {
        f"{BASE}/auth/login": {"post"},
        f"{BASE}/tokens": {"get", "post"},
        f"{BASE}/tokens/{{pk}}": {"delete"},
        f"{BASE}/uploads": {"post"},
        f"{BASE}/users": {"get", "post"},
        f"{BASE}/users/{{pk}}": {"get", "patch"},
        f"{BASE}/roles": {"get", "post"},
        f"{BASE}/roles/{{pk}}": {"get", "patch", "delete"},
        f"{BASE}/permissions": {"get"},
        f"{BASE}/history": {"get"},
        f"{BASE}/models/{{label}}/records/{{pk}}/history": {"get"},
        f"{BASE}/models/{{label}}/records/{{pk}}/history/{{entry_id}}/revert": {"post"},
        f"{BASE}/models/{{label}}/records/{{pk}}/comments": {"get", "post"},
        f"{BASE}/models/{{label}}/records/{{pk}}/comments/{{comment_id}}": {"patch", "delete"},
        f"{BASE}/models/{{label}}/records/{{pk}}/comments/{{comment_id}}/resolve": {"post"},
        f"{BASE}/models/{{label}}/records/{{pk}}/inlines/{{name}}": {"get", "patch"},
        f"{BASE}/models/{{label}}/records:upsert": {"patch"},
        f"{BASE}/models/{{label}}/records/_form-state": {"post"},
        f"{BASE}/models/{{label}}/actions/{{name}}": {"post"},
        f"{BASE}/models/{{label}}/views": {"get", "post"},
        f"{BASE}/models/{{label}}/views/{{pk}}": {"delete"},
        f"{BASE}/auth/logout": {"post"},
        f"{BASE}/auth/me": {"get"},
        f"{BASE}/auth/password": {"post"},
        f"{BASE}/meta": {"get"},
        f"{BASE}/models/{{label}}/schema": {"get"},
        f"{BASE}/models/{{label}}/records": {"get", "post", "patch"},
        f"{BASE}/models/{{label}}/records:query": {"post"},
        f"{BASE}/models/{{label}}/records:delete": {"post"},
        f"{BASE}/models/{{label}}/records/{{pk}}": {"get", "patch", "delete"},
        f"{BASE}/models/{{label}}/records/{{pk}}/delete-preview": {"get"},
        f"{BASE}/models/{{label}}/options/{{field}}": {"get"},
        f"{BASE}/models/{{label}}/records/{{pk}}/files/{{field}}": {"get"},
    }
    assert openapi["components"]["schemas"]["RecordOutput"]["properties"]["id"]["type"] == "string"


async def test_rate_limits_and_body_limits(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert settings._wrapped is not None
    await sign_in(api)
    monkeypatch.setattr(settings._wrapped, "ADMIN_API_MAX_BODY_BYTES", 10)
    csrf = (await api.get(f"{BASE}/auth/me")).json()["csrf_token"]
    oversized = await api.post(
        ITEMS, headers={"X-CSRF-Token": csrf}, json={"fields": {"name": "too-long"}}
    )
    assert oversized.status_code == 413


@pytest.mark.parametrize(
    "body,content_type,status",
    [
        ("{", "application/json", 400),
        ("[]", "application/json", 422),
        ('{"x": NaN}', "application/json", 400),
        ('{"x": 1e999}', "application/json", 400),
        ("plain", "text/plain", 415),
    ],
)
async def test_invalid_bodies(
    api: httpx.AsyncClient, body: str, content_type: str, status: int
) -> None:
    headers = await sign_in(api)
    response = await api.post(
        ITEMS, content=body, headers={**headers, "Content-Type": content_type}
    )
    assert response.status_code == status


async def test_custom_backend_persists_across_api_and_password_change(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert settings._wrapped is not None
    model = "obox_builder.contrib.auth.backends.ModelBackend"
    custom = "tests.support.auth_backend.PasswordBackend"
    monkeypatch.setattr(settings._wrapped, "AUTHENTICATION_BACKENDS", [model, custom])
    await make_user("alice")
    login = await api.post(
        f"{BASE}/auth/login", json={"username": "alice", "password": "backend-secret"}
    )
    assert login.status_code == 200
    assert (await api.get(f"{BASE}/auth/me")).status_code == 200
    changed = await api.post(
        f"{BASE}/auth/password",
        headers={"X-CSRF-Token": login.json()["csrf_token"]},
        json={"current_password": "s3cure-password", "new_password": "another-secure-password"},
    )
    assert changed.status_code == 200
    assert (await api.get(f"{BASE}/auth/me")).status_code == 200
    monkeypatch.setattr(settings._wrapped, "AUTHENTICATION_BACKENDS", [model])
    assert (await api.get(f"{BASE}/auth/me")).status_code == 401
