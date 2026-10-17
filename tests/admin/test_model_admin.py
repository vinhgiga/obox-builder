from __future__ import annotations

from typing import Any, cast

import pytest
from obox_builder.contrib.admin import AdminContext, ModelAdmin, site
from obox_builder.contrib.admin.options import pk_value

from tests.support.apps.shop.admin import ProductAdmin
from tests.support.apps.shop.models import Category, Product

pytestmark = pytest.mark.usefixtures("admin_env")


class StubUser:
    def __init__(self, *permissions: str) -> None:
        self.permissions = set(permissions)

    def has_perm(self, perm: str) -> bool:
        return perm in self.permissions


def admin_ctx(*permissions: str) -> AdminContext:
    dummy_request = cast(Any, None)
    return AdminContext(
        request=dummy_request, user=StubUser(*permissions), db=cast(Any, None), site=site
    )


def test_identity_and_paths() -> None:
    admin = ProductAdmin(Product, site)
    assert admin.app_label == "shop"
    assert admin.model_name == "product"
    assert admin.model_label == "shop.product"
    assert admin.name == "Product"
    assert admin.plural_name == "Products"
    assert admin.list_path() == "/admin/shop/product"
    assert admin.add_path() == "/admin/shop/product/add"
    assert admin.change_path(7) == "/admin/shop/product/7"
    assert admin.delete_path(7) == "/admin/shop/product/7/delete"
    assert admin.route_prefix == "admin:shop_product"


def test_irregular_plurals() -> None:
    admin = ProductAdmin(Category, site)
    assert admin.plural_name == "Categories"


def test_base_path_overrides_the_registered_urls() -> None:
    class PostsAdmin(ProductAdmin):
        base_path = "blog/posts"

    admin = PostsAdmin(Product, site)
    assert admin.path_prefix == "blog/posts"
    assert admin.list_path() == "/admin/blog/posts"
    assert admin.add_path() == "/admin/blog/posts/add"
    assert admin.change_path(7) == "/admin/blog/posts/7"
    assert admin.delete_path(7) == "/admin/blog/posts/7/delete"


def test_base_path_is_normalized() -> None:
    class PostsAdmin(ProductAdmin):
        base_path = "/posts/"

    assert PostsAdmin(Product, site).list_path() == "/admin/posts"


def test_invalid_base_path_is_reported() -> None:
    class BadAdmin(ProductAdmin):
        base_path = "Blog Posts"

    errors = [
        message for message in BadAdmin(Product, site).validate() if message.id == "obox.admin.E106"
    ]
    assert len(errors) == 1


def test_permission_names_follow_the_model() -> None:
    admin = ProductAdmin(Product, site)
    assert admin.permission_name("view") == "shop.view_product"
    assert admin.permission_name("add") == "shop.add_product"


def test_permission_hooks_delegate_to_the_user() -> None:
    admin = ProductAdmin(Product, site)
    ctx = admin_ctx("shop.view_product")
    assert admin.has_view_permission(ctx) is True
    assert admin.has_add_permission(ctx) is False
    no_perms = admin_ctx()
    assert admin.has_view_permission(no_perms) is False


def test_default_ordering_is_the_primary_key() -> None:
    admin = ModelAdmin(Product, site)
    assert admin.get_ordering(cast(Any, None)) == ("id",)
    assert ProductAdmin(Product, site).get_ordering(cast(Any, None)) == ("-id",)


def test_list_specs_follow_list_display() -> None:
    admin = ProductAdmin(Product, site)
    assert [spec.name for spec in admin.get_list_specs()] == [
        "id",
        "name",
        "status",
        "price_cents",
        "category_id",
        "is_active",
    ]


def test_list_specs_default_to_pk_plus_five_columns() -> None:
    admin = ModelAdmin(Product, site)
    names = [spec.name for spec in admin.get_list_specs()]
    assert names[0] == "id"
    assert all(spec.secret is False for spec in admin.get_list_specs())


def test_fields_restricts_the_form_and_schema_excludes_readonly() -> None:
    class NarrowAdmin(ModelAdmin):
        fields = ("name", "price_cents", "meta")

    admin = NarrowAdmin(Product, site)
    assert [spec.name for spec in admin.get_form_specs()] == ["name", "price_cents", "meta"]
    schema = admin.get_form_schema("add")
    assert set(schema.model_fields) == {"name", "price_cents"}


def test_readonly_and_exclude_are_respected() -> None:
    class TightAdmin(ModelAdmin):
        readonly_fields = ("name",)
        exclude = ("meta",)

    admin = TightAdmin(Product, site)
    names = [spec.name for spec in admin.get_field_specs()]
    assert "meta" not in names
    assert admin.get_spec("name").readonly is True  # type: ignore[union-attr]
    assert admin.get_spec("name").editable is False  # type: ignore[union-attr]


def test_queryset_eager_loads_link_relationships() -> None:
    admin = ProductAdmin(Product, site)
    stmt = admin.get_queryset(admin_ctx())
    assert stmt._with_options


def test_get_pk_and_parse_pk() -> None:
    admin = ProductAdmin(Product, site)
    product = Product(name="Widget")
    product.id = 42
    assert admin.get_pk(product) == 42
    assert admin.parse_pk("42") == 42
    with pytest.raises(LookupError, match="Invalid product id"):
        admin.parse_pk("nope")


def test_pk_value_accepts_scalars_and_instances() -> None:
    assert pk_value(5) == 5
    product = Product(name="Widget")
    product.id = 9
    assert pk_value(product) == 9
    assert pk_value(None) is None


def test_validate_is_clean_for_the_shipped_admins() -> None:
    from tests.support.apps.shop.admin import CategoryAdmin

    assert ProductAdmin(Product, site).validate() == []
    assert CategoryAdmin(Category, site).validate() == []


def test_validate_reports_broken_declarations() -> None:
    class BrokenAdmin(ModelAdmin):
        list_display = ("nope",)
        search_fields = ("category.name",)
        list_filter = ("missing",)
        ordering = ("-missing",)

    ids = {message.id for message in BrokenAdmin(Product, site).validate()}
    assert {"obox.admin.E102", "obox.admin.E103", "obox.admin.E104", "obox.admin.E105"} <= ids


def test_validate_flags_unindexed_search_and_unsearchable_type() -> None:
    class NoSearch(ProductAdmin):
        search_fields = ("meta", "is_active")

    class Unindexed(ModelAdmin):
        search_fields = ("name",)

    messages = NoSearch(Product, site).validate() + Unindexed(Category, site).validate()
    ids = [(message.id, message.msg) for message in messages]
    assert any(id_ == "obox.admin.E103" and "'meta'" in msg for id_, msg in ids)
    assert any(id_ == "obox.admin.E103" and "'is_active'" in msg for id_, msg in ids)
    assert any(id_ == "obox.admin.W101" and "'name'" in msg for id_, msg in ids)
