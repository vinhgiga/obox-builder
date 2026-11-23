import asyncio
import re
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi import Depends, FastAPI
from obox import Obox, forms, permission_required
from obox.core.exceptions import ImproperlyConfigured
from obox.core.principal import Principal
from obox.db.session import _session
from obox.db.transaction import atomic
from obox.views.generic import CreateView
from pydantic import BaseModel
from sqlalchemy.orm import Mapped, mapped_column, relationship, selectinload, with_loader_criteria
from starlette.datastructures import FormData

from tests.test_forms_integration import Base, ManualKey, configured

product_tags = sa.Table(
    "form_product_tags",
    Base.metadata,
    sa.Column("product_id", sa.ForeignKey("form_products.id"), primary_key=True),
    sa.Column("tag_id", sa.ForeignKey("form_tags.id"), primary_key=True),
)
scored_tags = sa.Table(
    "form_scored_tags",
    Base.metadata,
    sa.Column("product_id", sa.ForeignKey("form_products.id"), primary_key=True),
    sa.Column("tag_id", sa.ForeignKey("form_tags.id"), primary_key=True),
    sa.Column("score", sa.Integer, nullable=False),
)


class Category(Base):
    __tablename__ = "form_categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[int]
    active: Mapped[bool]
    name: Mapped[str] = mapped_column(sa.String(40))
    products: Mapped[list["Product"]] = relationship(back_populates="category", lazy="raise")

    def __str__(self) -> str:
        return self.name


class Tag(Base):
    __tablename__ = "form_tags"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[int]
    active: Mapped[bool]
    name: Mapped[str] = mapped_column(sa.String(40))

    def __str__(self) -> str:
        return self.name


class Product(Base):
    __tablename__ = "form_products"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(40))
    category_id: Mapped[int] = mapped_column(sa.ForeignKey(Category.id))
    category: Mapped[Category] = relationship(back_populates="products", lazy="raise")
    featured_id: Mapped[int | None] = mapped_column(sa.ForeignKey(Tag.id), unique=True)
    featured: Mapped[Tag | None] = relationship()
    tags: Mapped[list[Tag]] = relationship(secondary=product_tags, lazy="raise")
    scored: Mapped[list[Tag]] = relationship(secondary=scored_tags)
    visible_tags: Mapped[list[Tag]] = relationship(secondary=product_tags, viewonly=True)


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = ("name", "category", "featured", "tags")

    def __init__(self, *args: Any, tenant: int = 1, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        cast(forms.ModelChoiceField, self.fields["category"]).queryset = sa.select(Category).where(
            Category.tenant == tenant, Category.active.is_(True)
        )
        for name in ("featured", "tags"):
            cast(forms.ModelChoiceField, self.fields[name]).queryset = sa.select(Tag).where(
                Tag.tenant == tenant, Tag.active.is_(True)
            )


class Token(Base):
    __tablename__ = "form_tokens"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)


class DefaultProduct(Base):
    __tablename__ = "form_default_products"
    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(sa.ForeignKey(Category.id), default=1)
    category: Mapped[Category] = relationship()


async def seed() -> None:
    async with _session() as session:
        for model in (Category, Tag):
            session.add_all(
                [
                    model(id=1, tenant=1, active=True, name="Allowed <one>"),
                    model(id=2, tenant=1, active=True, name="Allowed two"),
                    model(id=3, tenant=2, active=True, name="Other tenant"),
                    model(id=4, tenant=1, active=False, name="Inactive"),
                ]
            )


def test_unbound_model_form_and_missing_bound_values_need_no_database() -> None:
    class ContactInput(BaseModel):
        name: str

    class Contact(forms.Form[ContactInput]):
        schema = ContactInput

    async def scenario() -> None:
        form = ProductForm()
        assert not await form.is_valid()
        with pytest.raises(ValueError, match="could not be saved"):
            await form.save()
        missing = Contact({})["name"]
        assert missing.value() is None
        assert "value=" not in str(missing)

    asyncio.run(scenario())


def test_generated_fields_metadata_and_scalar_backwards_compatibility() -> None:
    fields = forms.fields_for_model(
        Product,
        ("category", "featured", "tags"),
        labels={"category": "Group"},
        help_texts={"tags": "Pick tags"},
    )
    assert isinstance(fields["category"], forms.ModelChoiceField)
    assert fields["category"].required
    assert fields["category"].label == "Group"
    assert not fields["featured"].required
    assert isinstance(fields["tags"], forms.ModelMultipleChoiceField)
    assert isinstance(fields["tags"].widget, forms.SelectMultiple)
    assert fields["tags"].help_text == "Pick tags"
    assert isinstance(
        forms.fields_for_model(Product, ("category_id",))["category_id"], forms.IntegerField
    )
    all_fields = forms.fields_for_model(Product, "__all__", exclude=("scored",))
    assert {"category", "featured", "tags"} <= all_fields.keys()
    assert not {"category_id", "featured_id", "visible_tags"} & all_fields.keys()
    assert "products" not in forms.fields_for_model(Category)


@pytest.mark.parametrize("names", [("category", "category_id"), ("scored",), ("visible_tags",)])
def test_unsupported_or_conflicting_relationships_fail_explicitly(names: tuple[str, ...]) -> None:
    with pytest.raises(ImproperlyConfigured):
        forms.fields_for_model(Product, names)
    with pytest.raises(ImproperlyConfigured):
        forms.fields_for_model(Category, ("products",))


def test_declared_scalar_cannot_bypass_the_relationship_scope() -> None:
    with pytest.raises(ImproperlyConfigured, match="not both"):

        class Conflicting(ProductForm):
            category_id = forms.IntegerField()


@pytest.mark.parametrize("field", ["category", "featured", "tags"])
@pytest.mark.parametrize("value", ["3", "4", "999", "bad", {"id": 1}])
def test_choices_reject_inaccessible_missing_and_malformed_ids(field: str, value: Any) -> None:
    async def scenario() -> None:
        async with configured():
            await seed()
            data = {
                "name": "Product",
                "category": "1",
                field: [value] if field == "tags" else value,
            }
            form = ProductForm(data)
            assert not await form.is_valid()
            assert form.has_error(field, "invalid_choice")
            with pytest.raises(ValueError, match="could not be saved"):
                await form.save()
            async with _session() as session:
                assert await session.scalar(sa.select(sa.func.count()).select_from(Product)) == 0

    asyncio.run(scenario())


def test_create_edit_and_clear_relationships_without_implicit_async_io() -> None:
    async def scenario() -> None:
        async with configured():
            await seed()
            form = ProductForm(
                {"name": "First", "category": "1", "featured": "1", "tags": ["1", "2", "1"]}
            )
            assert await form.is_valid()
            assert isinstance(form.validated_data["category"], Category)
            assert [tag.id for tag in form.validated_data["tags"]] == [1, 2]
            assert form.validated_data["featured"] is form.validated_data["tags"][0]
            product = await form.save()
            assert product.category_id == product.category.id == 1
            assert [tag.id for tag in product.tags] == [1, 2]
            async with _session() as session:
                detached = await session.get(Product, product.id)
            edit = ProductForm(
                {"name": "Edited", "category": "2", "tags": ["2"]}, instance=detached
            )
            await edit.prepare()
            assert edit.initial["category"].id == 1
            assert {tag.id for tag in edit.initial["tags"]} == {1, 2}
            assert await edit.is_valid()
            assert detached is not None
            assert detached.name == "First"
            assert (await edit.save()) is detached
            await edit.save_m2m()  # Repeated calls must not duplicate links.
            clear = ProductForm({"name": "Cleared", "category": "1"}, instance=detached)
            await clear.save()
            async with _session() as session:
                saved = await session.scalar(sa.select(Product).options(selectinload(Product.tags)))
                assert saved is not None
                assert saved.name == "Cleared"
                assert saved.category_id == 1
                assert saved.featured_id is None
                assert saved.tags == []
                assert await session.scalar(sa.select(sa.func.count()).select_from(Tag)) == 4

    asyncio.run(scenario())


def test_commit_false_defers_many_to_many_and_atomic_rollback() -> None:
    async def scenario() -> None:
        async with configured():
            await seed()
            form = ProductForm({"name": "Deferred", "category": "1", "tags": ["1", "2"]})
            product = await form.save(commit=False)
            assert product.id is None
            assert product.category_id == 1
            with pytest.raises(ValueError, match="Save the instance"):
                await form.save_m2m()
            async with _session() as session:
                session.add(product)
                await session.flush()
                assert (
                    await session.scalar(sa.select(sa.func.count()).select_from(product_tags)) == 0
                )
            await form.save_m2m()
            async with _session() as session:
                assert (
                    await session.scalar(sa.select(sa.func.count()).select_from(product_tags)) == 2
                )
            with pytest.raises(RuntimeError, match="rollback"):  # noqa: PT012
                async with atomic():
                    await ProductForm(
                        {"name": "Rolled back", "category": "2", "tags": ["2"]}
                    ).save()
                    raise RuntimeError("rollback")
            async with _session() as session:
                assert (await session.scalars(sa.select(Product.name))).all() == ["Deferred"]
                assert (
                    await session.scalar(sa.select(sa.func.count()).select_from(product_tags)) == 2
                )

    asyncio.run(scenario())


def test_one_to_one_uniqueness_is_reported_on_the_relationship_field() -> None:
    async def scenario() -> None:
        async with configured():
            await seed()
            product = await ProductForm({"name": "First", "category": "1", "featured": "1"}).save()
            duplicate = ProductForm({"name": "Second", "category": "1", "featured": "1"})
            assert not await duplicate.is_valid()
            assert "already exists" in duplicate.errors["featured"][0]
            unchanged = ProductForm(
                {"name": "Edited", "category": "1", "featured": "1"}, instance=product
            )
            assert await unchanged.is_valid()

    asyncio.run(scenario())


def test_prefix_repeated_post_values_disabled_initials_and_field_isolation() -> None:
    async def scenario() -> None:
        async with configured():
            await seed()
            first, other = ProductForm(), ProductForm(tenant=2)
            await first.prepare()
            await other.prepare()
            assert {
                key for key, _ in cast(forms.ModelChoiceField, first.fields["tags"]).choices
            } == {1, 2}
            assert {
                key for key, _ in cast(forms.ModelChoiceField, other.fields["tags"]).choices
            } == {3}
            assert "&lt;one&gt;" in str(first["category"])
            product = await ProductForm(
                {"name": "First", "category": "1", "tags": ["1", "2"]}
            ).save()
            async with _session() as session:
                detached = await session.get(Product, product.id)
            form = ProductForm(
                FormData(
                    [
                        ("p-name", "Edited"),
                        ("p-category", "3"),
                        ("p-tags", "1"),
                        ("p-tags", "2"),
                    ]
                ),
                instance=detached,
                prefix="p",
            )
            form.fields["category"].disabled = True
            assert await form.is_valid()
            assert form.validated_data["category"].id == 1
            rendered = str(form["tags"])
            assert "multiple" in rendered
            assert 'name="p-tags"' in rendered
            assert 'value="1" selected' in rendered
            assert 'value="2" selected' in rendered
            assert len((await form.save()).tags) == 2
            invalid_list = ProductForm({"name": "P", "category": "1", "tags": "1"})
            assert not await invalid_list.is_valid()
            assert invalid_list.has_error("tags", "invalid_list")
            first.fields["tags"].required = True
            first.data, first.is_bound = {"name": "P", "category": "1"}, True
            assert not await first.is_valid()
            assert first.has_error("tags", "required")

    asyncio.run(scenario())


def test_declared_model_choices_preserve_select_limits_and_return_model_objects() -> None:
    class KeyForm(forms.ModelForm):
        key = forms.ModelChoiceField(sa.select(ManualKey).order_by(ManualKey.code).limit(1))

        class Meta:
            model = ManualKey
            fields = ()

    async def scenario() -> None:
        async with configured():
            async with _session() as session:
                session.add_all([ManualKey(code="a", label="A"), ManualKey(code="b", label="B")])
            valid = KeyForm({"key": "a"})
            assert await valid.is_valid()
            assert valid.validated_data["key"].code == "a"
            assert not await KeyForm({"key": "b"}).is_valid()

    asyncio.run(scenario())


@pytest.mark.parametrize("limited", [False, True])
def test_orm_policy_options_apply_to_display_and_validation(limited: bool) -> None:
    async def scenario() -> None:
        async with configured():
            await seed()
            query = (
                sa.select(Category)
                .options(
                    with_loader_criteria(
                        Category,
                        Category.tenant == 1,
                    )
                )
                .where(Category.active.is_(True))
                .order_by(Category.id)
            )
            if limited:
                query = query.limit(1)
            field = forms.ModelChoiceField(query)
            await field.prepare()
            assert {key for key, _ in field.choices} == ({"", 1} if limited else {"", 1, 2})
            assert (await field.clean("1")).id == 1
            for value in ("3", "4"):
                with pytest.raises(forms.ValidationError):
                    await field.clean(value)

    asyncio.run(scenario())


def test_validation_queries_current_scope_instead_of_cached_display_choices() -> None:
    async def scenario() -> None:
        async with configured():
            await seed()
            form = ProductForm({"name": "P", "category": "1", "tags": ["1"]})
            await form.prepare()
            async with _session() as session:
                await session.execute(
                    sa.update(Category).where(Category.id == 1).values(active=False)
                )
                await session.execute(sa.update(Tag).where(Tag.id == 1).values(active=False))
            assert not await form.is_valid()
            assert form.has_error("category", "invalid_choice")
            assert form.has_error("tags", "invalid_choice")

    asyncio.run(scenario())


def test_uuid_keys_optional_fields_validators_and_custom_errors() -> None:
    async def scenario() -> None:
        async with configured():
            token = Token()
            async with _session() as session:
                session.add(token)
                await session.flush()
            validated: list[Token] = []
            field = forms.ModelChoiceField(
                sa.select(Token),
                validators=[validated.append],
                error_messages={"invalid_choice": "Unavailable"},
            )
            result = await field.clean(str(token.id))
            assert result.id == token.id
            assert validated == [result]
            with pytest.raises(forms.ValidationError, match="Unavailable"):
                await field.clean("bad")
            optional = forms.ModelMultipleChoiceField(sa.select(Token), required=False)
            assert await optional.clean(None) == []
            assert [obj.id for obj in await optional.clean([token.id, str(token.id)])] == [token.id]
            optional_single = forms.ModelChoiceField(sa.select(Token), required=False)
            assert await optional_single.clean("") is None
            required = forms.ModelChoiceField(sa.select(Token))
            with pytest.raises(forms.ValidationError, match="required"):
                await required.clean(None)

    asyncio.run(scenario())


def test_default_foreign_key_and_explicit_empty_are_distinguished() -> None:
    form_class = forms.modelform_factory(DefaultProduct, fields=("category",))

    async def scenario() -> None:
        async with configured():
            await seed()
            product = await form_class({}).save()
            assert product.category_id == 1
            pending = await form_class({"category": ""}).save(commit=False)
            assert pending.category_id is None

    asyncio.run(scenario())


def test_commit_false_edit_defers_link_changes_and_rollback_restores_links() -> None:
    async def scenario() -> None:
        async with configured():
            await seed()
            product = await ProductForm({"name": "P", "category": "1", "tags": ["1"]}).save()
            edit = ProductForm({"name": "Edited", "category": "2", "tags": ["2"]}, instance=product)
            await edit.save(commit=False)
            async with _session() as session:
                assert await session.scalar(sa.select(Product.name)) == "P"
                assert (await session.scalars(sa.select(product_tags.c.tag_id))).all() == [1]
                session.add(product)
                await session.flush()
            with pytest.raises(RuntimeError, match="rollback"):  # noqa: PT012
                async with atomic():
                    await edit.save_m2m()
                    raise RuntimeError("rollback")
            async with _session() as session:
                assert await session.scalar(sa.select(Product.name)) == "Edited"
                assert (await session.scalars(sa.select(product_tags.c.tag_id))).all() == [1]

    asyncio.run(scenario())


def test_generic_view_prepares_scoped_choices_and_checks_forged_post() -> None:
    class Create(CreateView):
        model = Product
        form_class = ProductForm
        template_name = "obox/generic/form.html"
        success_url = "/done/"

        async def get_form_kwargs(self) -> dict[str, Any]:
            return {**await super().get_form_kwargs(), "tenant": 1}

    async def scenario() -> None:
        async with configured():
            await seed()
            from tests.test_registered_views import Actors

            app = FastAPI(openapi_url=None)
            app.add_middleware(
                Actors, actor=Principal(id=1, is_authenticated=True, is_superuser=True)
            )
            Obox(app)
            app.add_api_route(
                "/new/",
                Create.as_view(),
                methods=["GET", "POST"],
                dependencies=[Depends(permission_required("tests.use_forms", html=True))],
            )
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client,
            ):
                page = await client.get("/new/")
                assert page.status_code == 200
                assert "Allowed two" in page.text
                assert "Other tenant" not in page.text
                assert "Inactive" not in page.text
                token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
                assert token is not None
                bad = await client.post(
                    "/new/",
                    data={
                        "csrfmiddlewaretoken": token[1],
                        "name": "P",
                        "category": "3",
                        "tags": ["1", "4"],
                    },
                )
                assert bad.status_code == 200
                assert "Select a valid choice" in bad.text
                good = await client.post(
                    "/new/",
                    data={
                        "csrfmiddlewaretoken": token[1],
                        "name": "P",
                        "category": "1",
                        "tags": ["1", "2"],
                    },
                )
                assert good.status_code == 303
                async with _session() as session:
                    assert (
                        await session.scalar(sa.select(sa.func.count()).select_from(product_tags))
                        == 2
                    )

    asyncio.run(scenario())
