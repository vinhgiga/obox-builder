import asyncio

import pytest
import sqlalchemy as sa
from obox import forms
from obox.db.session import session_scope
from obox.db.transaction import atomic
from pydantic import BaseModel
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tests.runtime import resource_for
from tests.test_forms_integration import Base, configured


class Author(Base):
    __tablename__ = "inline_authors"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    books: Mapped[list["Book"]] = relationship(back_populates="author", lazy="raise")


class Book(Base):
    __tablename__ = "inline_books"
    __table_args__ = (sa.UniqueConstraint("author_id", "title"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(sa.ForeignKey(Author.id))
    author: Mapped[Author] = relationship(back_populates="books", lazy="raise")
    title: Mapped[str] = mapped_column(sa.String(40))


Books = forms.inlineformset_factory(Author, resource_for(Book, fields=("title",)))


def data(*rows: dict[str, str], initial: int = 0, prefix: str = "books") -> dict[str, str]:
    return {
        f"{prefix}-TOTAL_FORMS": str(len(rows)),
        f"{prefix}-INITIAL_FORMS": str(initial),
        **{
            f"{prefix}-{index}-{key}": value
            for index, row in enumerate(rows)
            for key, value in row.items()
        },
    }


async def seed() -> Author:
    async with session_scope() as session:
        author = Author(name="One")
        session.add(author)
        await session.flush()
        session.add_all(
            [Book(author_id=author.id, title="First"), Book(author_id=author.id, title="Second")]
        )
    return author


def test_inline_load_add_update_delete_and_resubmit() -> None:
    async def scenario() -> None:
        async with configured():
            author = await seed()
            unbound = Books(instance=author)
            await unbound.prepare()
            assert unbound.get_default_prefix() == "books"
            assert len(unbound) == 5
            assert len(unbound.initial_forms) == 2
            assert not await unbound.is_valid()
            assert 'name="books-TOTAL_FORMS"' in "".join(
                str(field) for field in unbound.management_form
            )
            assert "books-__prefix__-title" in "".join(str(field) for field in unbound.empty_form)
            assert "required" not in str(unbound.empty_form["title"])
            edit = Books(
                data(
                    {"id": "1", "title": "Changed"},
                    {"id": "2", "DELETE": "on"},
                    {"title": "Third"},
                    {},
                    initial=2,
                ),
                instance=author,
            )
            assert await edit.is_valid(), (edit.errors, edit.non_form_errors())
            result = await edit.save()
            assert len(result) == 2
            assert (
                len(edit.new_objects) == len(edit.deleted_objects) == len(edit.changed_objects) == 1
            )
            async with session_scope() as session:
                assert list(await session.scalars(sa.select(Book.title).order_by(Book.title))) == [
                    "Changed",
                    "Third",
                ]
            same = Books(data({"id": "1", "title": "Changed"}, initial=1), instance=author)
            assert await same.is_valid()
            assert len(await same.save()) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "row", [{"id": "999", "title": "Forged"}, {"title": "Forged", "author_id": "999"}]
)
def test_inline_rejects_unknown_identity_and_parent(row: dict[str, str]) -> None:
    async def scenario() -> None:
        async with configured():
            author = await seed()
            bound = Books(data(row, initial=int("id" in row)), instance=author)
            assert not await bound.is_valid()
            with pytest.raises(ValueError, match="didn't validate"):
                await bound.save()

    asyncio.run(scenario())


def test_inline_unsaved_parent_duplicates_and_custom_clean() -> None:
    class UniqueBooks(forms.BaseInlineFormSet):
        def clean(self) -> None:
            super().clean()
            if any(form.validated_data.get("title") == "Rejected" for form in self.forms):
                raise forms.ValidationError("Choose another title.")

    custom = forms.inlineformset_factory(
        Author, resource_for(Book, fields=("title",)), formset=UniqueBooks
    )

    async def scenario() -> None:
        async with configured():
            author = Author(name="New")
            duplicate = Books(data({"title": "Same"}, {"title": "Same"}), instance=author)
            assert not await duplicate.is_valid()
            assert duplicate.non_form_errors()
            rejected = custom(data({"title": "Rejected"}), instance=author)
            assert not await rejected.is_valid()
            assert rejected.non_form_errors() == ["Choose another title."]
            valid = Books(data({"title": "New"}), instance=author)
            assert await valid.is_valid()
            with pytest.raises(ValueError, match="parent"):
                await valid.save()
            async with atomic(), session_scope() as session:
                session.add(author)
                await session.flush()
                await valid.save()
            async with session_scope() as session:
                assert await session.scalar(sa.select(Book.author_id)) == author.id

    asyncio.run(scenario())


def test_formset_management_limits_ordering_and_deleted_invalid_rows() -> None:
    class ContactInput(BaseModel):
        name: str

    class Contact(forms.Form[ContactInput]):
        schema = ContactInput

    contacts = forms.formset_factory(
        Contact,
        can_delete=True,
        max_num=2,
        absolute_max=3,
        min_num=1,
        validate_min=True,
        validate_max=True,
    )

    async def scenario() -> None:
        missing = contacts({})
        assert not await missing.is_valid()
        assert "ManagementForm" in missing.non_form_errors()[0]
        huge = contacts({"form-TOTAL_FORMS": "10000000", "form-INITIAL_FORMS": "0"})
        assert not await huge.is_valid()
        assert len(huge) == 3
        valid = contacts(
            data(
                {"name": "Last", "ORDER": "2"},
                {"name": "First", "ORDER": "1"},
                {"DELETE": "on"},
                prefix="form",
            )
        )
        assert await valid.is_valid(), valid.errors
        assert [form.validated_data["name"] for form in valid.forms[:2]] == ["Last", "First"]
        assert len(valid.deleted_forms) == 1
        empty = contacts(data({}, prefix="form"))
        assert not await empty.is_valid()

    asyncio.run(scenario())


def test_inline_save_commit_false_and_transaction_rollback() -> None:
    async def scenario() -> None:
        async with configured():
            author = await seed()

            async def rollback() -> None:
                async with atomic():
                    fresh = Books(data({"title": "Rolled back"}), instance=author)
                    await fresh.save()
                    raise RuntimeError("rollback")

            with pytest.raises(RuntimeError, match="rollback"):
                await rollback()
            async with session_scope() as session:
                assert await session.scalar(sa.select(sa.func.count()).select_from(Book)) == 2

    asyncio.run(scenario())


class Link(Base):
    __tablename__ = "inline_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    left_id: Mapped[int] = mapped_column(sa.ForeignKey(Author.id))
    right_id: Mapped[int] = mapped_column(sa.ForeignKey(Author.id))
    left: Mapped[Author] = relationship(foreign_keys=[left_id])
    right: Mapped[Author] = relationship(foreign_keys=[right_id])
    label: Mapped[str]


def test_inline_disambiguates_foreign_keys_and_requires_explicit_fields() -> None:
    with pytest.raises(ValueError, match="fk_name"):
        forms.inlineformset_factory(Author, resource_for(Link, fields=("label", "right_id")))
    for name in ("left_id", "left"):
        cls = forms.inlineformset_factory(
            Author, resource_for(Link, fields=("label", "right_id")), fk_name=name
        )
        assert cls.fk_name == "left_id"
        assert cls.get_default_prefix() == "link_set"
    with pytest.raises(ValueError, match="foreign key"):
        forms.inlineformset_factory(Author, resource_for(Book, fields=("title",)), fk_name="title")
    default_fields = forms.inlineformset_factory(Author, resource_for(Book))
    assert default_fields.model is Book


@pytest.mark.parametrize(
    "management",
    [
        {},
        {"TOTAL_FORMS": "-1", "INITIAL_FORMS": "0"},
        {"TOTAL_FORMS": "invalid", "INITIAL_FORMS": "0"},
        {"TOTAL_FORMS": "1", "INITIAL_FORMS": "-1"},
    ],
)
def test_tampered_management_never_validates(management: dict[str, str]) -> None:
    async def scenario() -> None:
        async with configured():
            bound = Books(
                {f"books-{name}": value for name, value in management.items()},
                instance=Author(name="New"),
            )
            assert not await bound.is_valid()
            assert bound.non_form_errors()

    asyncio.run(scenario())


def test_integer_zero_extra_form_is_changed_and_saved_data_is_retained() -> None:
    class CountInput(BaseModel):
        value: int

    class Count(forms.Form[CountInput]):
        schema = CountInput

    async def scenario() -> None:
        bound = forms.formset_factory(Count)(data({"value": "0"}, prefix="form"))
        assert await bound.is_valid()
        assert bound.forms[0].has_changed()
        assert bound.cleaned_data == [{"value": 0}]

    asyncio.run(scenario())


def test_new_inline_commit_false_can_be_saved_with_unsaved_parent() -> None:
    async def scenario() -> None:
        async with configured():
            bound = Books(data({"title": "Pending child"}), instance=Author(name="Pending"))
            with pytest.raises(ValueError, match="Save the parent"):
                await bound.save()

    asyncio.run(scenario())


def test_inline_rejects_duplicate_existing_primary_keys() -> None:
    async def scenario() -> None:
        async with configured():
            parent = await seed()
            bound = Books(
                data({"id": "1", "title": "First"}, {"id": "1", "title": "Different"}, initial=2),
                instance=parent,
            )
            assert not await bound.is_valid()
            assert bound.non_form_errors()

    asyncio.run(scenario())
