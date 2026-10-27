from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from obox_builder.conf import settings
from obox_builder.db.session import session_scope
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.admin.api.conftest import sign_in, site_for
from tests.admin.api.test_phase3 import mint
from tests.admin.api.test_records import BASE, ITEMS, create_item


@pytest.fixture
def file_fields(api: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from tests.support.apps.warehouse.models import Item

    assert settings._wrapped is not None
    monkeypatch.setattr(settings._wrapped, "MEDIA_ROOT", tmp_path / "media")
    admin = site_for(api).get_model_admin_for(Item)
    monkeypatch.setattr(
        admin, "fields_config", {"internal_note": {"type": "file"}, "notes": {"type": "file"}}
    )
    monkeypatch.setattr(admin, "_specs", None)
    return tmp_path / "media"


async def upload(
    api: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    field: str = "internal_note",
    content: bytes = b"private text",
) -> str:
    response = await api.post(
        f"{BASE}/uploads",
        headers=headers,
        data={"model": "warehouse.item", "field": field},
        files={"file": ("private.txt", content, "text/plain")},
    )
    assert response.status_code == 201, response.text
    return response.json()["upload_token"]


async def test_staged_upload_attach_download_and_replay(
    api: httpx.AsyncClient, file_fields: Path
) -> None:
    headers = await sign_in(api)
    claim = await upload(api, headers)
    created = await create_item(
        api, {**headers, "Idempotency-Key": "file-record"}, internal_note={"upload_token": claim}
    )
    file = created.json()["fields"]["internal_note"]
    assert set(file) == {"name", "url"}
    response = await api.get(file["url"])
    assert response.status_code == 200
    assert response.content == b"private text"
    await create_item(
        api, {**headers, "Idempotency-Key": "file-record"}, internal_note={"upload_token": claim}
    )
    assert len(list((file_fields / "admin" / "uploads").iterdir())) == 1
    assert len(list((file_fields / "admin" / "api-staged").iterdir())) == 1


async def test_api_uploads_use_configured_storage(
    api: httpx.AsyncClient, file_fields: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.support.storage_backend import MemoryStorage

    assert settings._wrapped is not None
    monkeypatch.setattr(
        settings._wrapped, "STORAGE_BACKEND", "tests.support.storage_backend.MemoryStorage"
    )
    monkeypatch.setattr(MemoryStorage, "files", {})
    headers = await sign_in(api)
    claim = await upload(api, headers)
    created = await create_item(api, headers, internal_note={"upload_token": claim})
    assert created.status_code == 201, created.text
    response = await api.get(created.json()["fields"]["internal_note"]["url"])
    assert response.status_code == 200
    assert response.content == b"private text"
    assert len(MemoryStorage.files) == 2
    assert not file_fields.exists()


async def test_upload_claim_tampering_field_owner_and_expiry(
    api: httpx.AsyncClient, file_fields: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obox_builder.contrib.admin.api import uploads

    headers = await sign_in(api)
    claim = await upload(api, headers)
    body = {"fields": {"name": "File", "sku": "file", "internal_note": {"upload_token": claim}}}
    tampered = {"fields": {**body["fields"], "internal_note": {"upload_token": claim + "bad"}}}
    assert (await api.post(ITEMS, headers=headers, json=tampered)).status_code == 422
    wrong_field = {"fields": {"name": "File", "sku": "file", "notes": {"upload_token": claim}}}
    assert (await api.post(ITEMS, headers=headers, json=wrong_field)).status_code == 422
    other = await sign_in(api, username="other-upload-owner")
    assert (await api.post(ITEMS, headers=other, json=body)).status_code == 422
    login = await api.post(
        f"{BASE}/auth/login", json={"username": "api-user", "password": "s3cure-password"}
    )
    owner = {"X-CSRF-Token": login.json()["csrf_token"]}
    clock = time.time()
    monkeypatch.setattr(
        uploads.time, "time", lambda: clock + settings.ADMIN_API_UPLOAD_TTL_SECONDS + 1
    )
    assert (await api.post(ITEMS, headers=owner, json=body)).status_code == 422
    assert not (file_fields / "admin" / "uploads").exists()


async def test_upload_limits_content_and_raw_paths(
    api: httpx.AsyncClient, file_fields: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await sign_in(api)
    assert settings._wrapped is not None
    monkeypatch.setattr(settings._wrapped, "ADMIN_MAX_UPLOAD_BYTES", 10)
    for filename, content in (("fake.png", b"executable\x00"), ("large.txt", b"a" * 11)):
        response = await api.post(
            f"{BASE}/uploads",
            headers=headers,
            data={"model": "warehouse.item", "field": "internal_note"},
            files={"file": (filename, content, "image/png")},
        )
        assert response.status_code == 422, response.text
    huge = await api.post(
        f"{BASE}/uploads",
        headers=headers,
        data={"model": "warehouse.item", "field": "internal_note"},
        files={"file": ("huge.txt", b"a" * 66000, "text/plain")},
    )
    assert huge.status_code == 413
    raw = await api.post(
        ITEMS,
        headers=headers,
        json={
            "fields": {"name": "Raw", "sku": "raw", "internal_note": "admin/uploads/invalid.txt"}
        },
    )
    assert raw.status_code == 422


async def test_uploads_roll_back_files_on_batch_and_commit_failure(
    api: httpx.AsyncClient, file_fields: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.support.apps.warehouse.models import Item

    headers = await sign_in(api)
    claim = await upload(api, headers)
    fields = {"name": "File", "sku": "file", "internal_note": {"upload_token": claim}}
    batch = await api.post(
        ITEMS,
        headers=headers,
        json={"records": [{"fields": fields}, {"fields": {"name": "Duplicate", "sku": "file"}}]},
    )
    assert batch.status_code == 422
    assert list((file_fields / "admin" / "uploads").iterdir()) == []
    commit = AsyncSession.commit

    async def fail(db: AsyncSession) -> None:
        raise IntegrityError("COMMIT", {}, RuntimeError("constraint"))

    monkeypatch.setattr(AsyncSession, "commit", fail)
    response = await api.post(ITEMS, headers=headers, json={"fields": fields})
    assert response.status_code == 409
    assert list((file_fields / "admin" / "uploads").iterdir()) == []
    monkeypatch.setattr(AsyncSession, "commit", commit)
    async with session_scope() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(Item)) == 0


async def test_upload_pat_scope_and_purge_preserves_attached_files(
    api: httpx.AsyncClient, file_fields: Path
) -> None:
    from obox_builder.contrib.admin.api.uploads import purge_staged

    headers = await sign_in(api)
    token = await mint(api, headers, ["warehouse.item:read", "warehouse.item:write"])
    bearer = {"Authorization": f"Bearer {token['token']}"}
    claim = await upload(api, bearer)
    created = await create_item(api, bearer, internal_note={"upload_token": claim})
    now = time.time()
    staged = list((file_fields / "admin" / "api-staged").iterdir())
    attached = list((file_fields / "admin" / "uploads").iterdir())
    for path in staged + attached:
        os.utime(path, (now - settings.ADMIN_API_UPLOAD_TTL_SECONDS - 10,) * 2)
    assert purge_staged(limit=1) == 1
    assert attached[0].is_file()
    assert (
        await api.get(created.json()["fields"]["internal_note"]["url"], headers=bearer)
    ).content == b"private text"


async def test_upload_budget_is_shared_across_fields(
    api: httpx.AsyncClient, file_fields: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await sign_in(api)
    assert settings._wrapped is not None
    monkeypatch.setattr(settings._wrapped, "ADMIN_MAX_UPLOAD_BYTES", 10)
    a = await upload(api, headers, content=b"123456")
    b = await upload(api, headers, field="notes", content=b"123456")
    response = await api.post(
        ITEMS,
        headers=headers,
        json={
            "fields": {
                "name": "Too large",
                "sku": "large",
                "internal_note": {"upload_token": a},
                "notes": {"upload_token": b},
            }
        },
    )
    assert response.status_code == 413, response.text
    assert (await api.get(ITEMS)).json()["records"] == []
    assert not (file_fields / "admin" / "uploads").exists()
