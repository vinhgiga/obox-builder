from __future__ import annotations

import pytest
from obox_builder.contrib.admin import AdminSite, ModelView, site
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.resources import get_registry

from tests.support.apps.shop.models import Category, Order, Product

pytestmark = pytest.mark.usefixtures("admin_env")


@pytest.fixture
def local_site() -> AdminSite:
    return AdminSite()


def test_register_as_decorator_and_function(local_site: AdminSite) -> None:
    @local_site.view(get_registry().for_model(Category))
    class CategoryAdmin(ModelView):
        list_display = ("id", "name")

    class OrderAdmin(ModelView):
        list_display = ("id", "reference")

    local_site.register(get_registry().for_model(Order), OrderAdmin)
    assert local_site.get_model_admin("shop", "category") is not None
    assert local_site.get_model_admin("shop", "order") is not None


def test_get_model_accepts_snake_and_camel_names(local_site: AdminSite) -> None:
    class CategoryAdmin(ModelView):
        pass

    local_site.register(get_registry().for_model(Category), CategoryAdmin)
    assert local_site.get_model_admin("shop", "category") is not None
    assert local_site.get_model_admin("shop", "Category") is not None
    assert local_site.get_model_admin("shop", "nope") is None


def test_registration_rejects_conflicts_until_explicitly_unbound(local_site: AdminSite) -> None:
    class First(ModelView):
        pass

    class Second(ModelView):
        list_display = ("id", "name")

    local_site.register(get_registry().for_model(Category), First)
    with pytest.raises(ImproperlyConfigured, match="already has a view binding"):
        local_site.register(get_registry().for_model(Category), Second)
    local_site.unregister(get_registry().for_model(Category))
    local_site.register(get_registry().for_model(Category), Second)
    registered = local_site.get_model_admin("shop", "category")
    assert registered is not None
    assert isinstance(registered, Second)


def test_unregister_removes_the_admin(local_site: AdminSite) -> None:
    class CategoryAdmin(ModelView):
        pass

    local_site.register(get_registry().for_model(Category), CategoryAdmin)
    local_site.unregister(get_registry().for_model(Category))
    assert local_site.get_model_admin("shop", "category") is None


def test_registering_a_non_modeladmin_raises(local_site: AdminSite) -> None:
    with pytest.raises(ImproperlyConfigured, match="extend ModelView"):

        @local_site.view(get_registry().for_model(Category))
        class Nope:
            pass


def test_nav_groups_include_registered_models(local_site: AdminSite) -> None:
    class ProductAdmin(ModelView):
        pass

    local_site.register(get_registry().for_model(Product), ProductAdmin)
    groups = {group.label: group for group in local_site.get_nav_groups()}
    assert "Shop" in groups
    labels = [item.label for item in groups["Shop"].items]
    assert "Products" in labels
    product_item = next(item for item in groups["Shop"].items if item.label == "Products")
    assert product_item.url == "/admin/shop/product"
    assert product_item.required_perm == "shop.product.read"


def test_autodiscovery_registers_the_shop_admin() -> None:
    registered = site.get_model_admin("shop", "product")
    assert registered is not None
    assert registered.search_fields == ("name",)
    assert site._discovered is True


def test_router_contains_generic_routes() -> None:
    router = site.get_router()
    names = {getattr(route, "name", "") for route in router.routes}
    assert {
        "admin:shop_product_list",
        "admin:shop_product_add",
        "admin:shop_product_add_submit",
        "admin:shop_product_change",
        "admin:shop_product_change_submit",
        "admin:shop_product_delete",
        "admin:shop_product_delete_submit",
        "admin:shop_category_list",
        "admin:shop_order_list",
    } <= names
    # Paths are relative to the ``/admin`` mount point.
    assert router.url_path_for("admin:shop_product_list") == "/shop/product"
    assert router.url_path_for("admin:shop_product_change", pk="7") == "/shop/product/7"


def test_router_uses_base_path_when_set() -> None:
    from tests.support.apps.shop.admin import ProductAdmin

    class PostsAdmin(ProductAdmin):
        base_path = "posts"

    site.unregister(get_registry().for_model(Product))
    site.register(get_registry().for_model(Product), PostsAdmin)
    try:
        router = site.get_router()
        assert router.url_path_for("admin:shop_product_list") == "/posts"
        assert router.url_path_for("admin:shop_product_change", pk="7") == "/posts/7"
    finally:
        site.unregister(get_registry().for_model(Product))
        site.register(get_registry().for_model(Product), ProductAdmin)


def test_system_check_reports_broken_admins() -> None:
    from obox_builder.contrib.admin.checks import check_model_admins
    from obox_builder.core.checks.messages import Error

    from tests.support.apps.shop.admin import CategoryAdmin

    class Broken(ModelView):
        list_display = ("nope",)

    site.unregister(get_registry().for_model(Category))
    site.register(get_registry().for_model(Category), Broken)
    try:
        messages = check_model_admins()
    finally:
        site.unregister(get_registry().for_model(Category))
        site.register(get_registry().for_model(Category), CategoryAdmin)
    assert any(
        isinstance(message, Error) and message.id == "obox.admin.E102" for message in messages
    )


def test_duplicate_list_paths_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    from obox_builder.contrib.admin import checks as checks_module

    class SamePath(ModelView):
        def list_path(self) -> str:
            return "/admin/collision"

    clashing = AdminSite()
    clashing.register(get_registry().for_model(Product), SamePath)
    clashing.register(get_registry().for_model(Category), SamePath)
    monkeypatch.setattr(checks_module, "site", clashing)
    messages = checks_module.check_model_admins()
    assert any("URLs would collide" in message.msg for message in messages)
