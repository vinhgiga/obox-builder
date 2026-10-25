from __future__ import annotations

from io import BytesIO
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from obox_builder.conf import settings
from obox_builder.contrib.admin import ModelAdmin, site
from obox_builder.contrib.admin.uploads import (
    discard_uploads,
    open_upload,
    persist_uploads,
    stage_uploads,
)
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.fields import FieldSpec
from obox_builder.fields.types.file import File
from starlette.datastructures import UploadFile

pytestmark = pytest.mark.usefixtures("admin_env")


def file_spec(**options: object) -> FieldSpec:
    return FieldSpec(
        name="asset",
        label="Asset",
        type=File(**options),
        column=sa.column("asset", sa.String()),
    )


async def test_private_upload_is_stored_with_random_name_and_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.load(), "MEDIA_ROOT", tmp_path)
    upload = UploadFile(filename="../../avatar.png", file=BytesIO(b"\x89PNG\r\n\x1a\nimage"))
    data, staged, errors = await stage_uploads({"asset": upload}, [file_spec()])

    assert errors == {}
    relative = data["asset"]
    assert relative.startswith("admin/uploads/")
    assert "avatar" not in relative
    persist_uploads(staged)
    stored = open_upload(relative)
    assert stored is not None
    assert stored.read().endswith(b"image")
    stored.close()
    path = tmp_path / relative
    assert path.stat().st_mode & 0o777 == 0o600
    discard_uploads(staged)
    assert open_upload(relative) is None


async def test_upload_rejects_content_that_does_not_match_supported_types() -> None:
    upload = UploadFile(filename="malware.png", file=BytesIO(b"MZ\0executable"))
    data, staged, errors = await stage_uploads({"asset": upload}, [file_spec()])

    assert data["asset"] is None
    assert staged == []
    assert "asset" in errors


async def test_upload_respects_field_byte_limit() -> None:
    upload = UploadFile(filename="large.png", file=BytesIO(b"\x89PNG\r\n\x1a\nimage"))
    _data, staged, errors = await stage_uploads({"asset": upload}, [file_spec(max_bytes=8)])

    assert staged == []
    assert errors["asset"] == ["File must be 8 bytes or smaller."]


def test_upload_storage_cannot_be_nested_in_static_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.load(), "MEDIA_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(settings.load(), "STATIC_ROOT", tmp_path)

    with pytest.raises(ImproperlyConfigured, match="public static directory"):
        persist_uploads([("admin/uploads/00000000000000000000000000000000.png", b"image")])


async def test_file_upload_and_download_require_record_view_permission(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.admin.conftest import (
        build_app,
        csrf_for,
        grant_permissions,
        login_session,
        make_user,
    )
    from tests.support.apps.shop.models import Product

    previous = site.get_model_admin_for(Product)

    class UploadAdmin(ModelAdmin):
        fields = ("name",)
        fields_config = {"name": File(accept=("image/png",), max_bytes=1024)}

    monkeypatch.setattr(settings.load(), "MEDIA_ROOT", tmp_path)
    site.register(Product, UploadAdmin)
    try:
        await make_user("upload_admin", is_superuser=True)
        transport = httpx.ASGITransport(app=build_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            session = await login_session(http, "upload_admin")
            token = await csrf_for("upload_admin", session)
            image = b"\x89PNG\r\n\x1a\nsmall test image"
            response = await http.post(
                "/admin/shop/product/add",
                data={"csrf_token": token},
                files={"name": ("unsafe/filename.png", image, "image/png")},
                follow_redirects=False,
            )
            assert response.status_code == 303
            from obox_builder.db.session import session_scope

            async with session_scope() as db:
                product = await db.scalar(
                    sa.select(Product).where(Product.name.like("admin/uploads/%"))
                )
            assert product is not None
            pk = product.id
            downloaded = await http.get(f"/admin/shop/product/{pk}/files/name")
            assert downloaded.content == image
            assert downloaded.headers["x-content-type-options"] == "nosniff"

            copied = await http.post(
                f"/admin/shop/product/{pk}",
                data={"csrf_token": token, "_action": "save_as_new"},
                follow_redirects=False,
            )
            assert copied.status_code == 303
            async with session_scope() as db:
                products = (await db.scalars(sa.select(Product).order_by(Product.id))).all()
                copied_pk = products[-1].id
            assert copied_pk != pk
            assert (await http.get(f"/admin/shop/product/{copied_pk}/files/name")).content == image

            await make_user("no_product_view")
            await login_session(http, "no_product_view")
            denied = await http.get(f"/admin/shop/product/{pk}/files/name")
            assert denied.status_code == 403

            await make_user("change_only")
            await grant_permissions("change_only", "shop.change_product")
            await login_session(http, "change_only")
            assert (await http.get(f"/admin/shop/product/{pk}/files/name")).content == image
    finally:
        if previous is not None:
            site.register(Product, type(previous))


def test_json_type_parses_and_bounds_user_input() -> None:
    from obox_builder.fields import introspect_model

    from tests.support.apps.shop.models import Product

    spec = next(field for field in introspect_model(Product) if field.name == "meta")
    value = spec.type.from_input('{"color":"blue"}', spec)
    assert value == {"color": "blue"}
    assert spec.type.validate(value, spec) is None
    assert spec.type.validate(spec.type.from_input("{bad", spec), spec) == "Enter valid JSON."
    assert spec.type.validate({"large": "x" * 70_000}, spec) is not None


def test_vietnamese_locale_formats_dates_and_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import datetime as dt
    from decimal import Decimal

    from obox_builder.fields.types.number import Decimal as DecimalField
    from obox_builder.fields.types.temporal import Date

    monkeypatch.setattr(settings.load(), "LANGUAGE_CODE", "vi")
    date = FieldSpec(name="date", label="Date", type=Date())
    number = FieldSpec(name="number", label="Number", type=DecimalField())
    assert date.type.cell(dt.date(2026, 10, 24), date).text == "24/10/2026"
    assert number.type.cell(Decimal("12345.60"), number).text == "12.345,60"
