"""Permission-aware display (ADR L10): history respects interface visibility."""

from __future__ import annotations

import uuid

import pytest
from obox_builder.contrib.admin import ModelView
from obox_builder.contrib.admin.views.history import DiffMask, diff_mask, entry_view
from obox_builder.contrib.history.models import RowChange
from obox_builder.contrib.history.service import history_entries
from obox_builder.resources import get_registry
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.apps.warehouse.models import Item


class User:
    def __init__(self, *, is_superuser: bool = False) -> None:
        self.is_superuser = is_superuser

    def has_perm(self, name: str) -> bool:
        return True


class RestrictedItemAdmin(ModelView):
    """Interface hides ``notes`` from the form (but keeps the column)."""

    list_display = ("id", "name", "quantity")
    fields = ("id", "name", "quantity")


class ExcludedItemAdmin(ModelView):
    """``notes`` was removed from the interface entirely (legacy name)."""

    list_display = ("id", "name", "quantity")
    exclude = ("notes",)


def test_admin_specs_drive_visibility(history_env: None) -> None:
    mask = diff_mask(Item, user=User(), admin=RestrictedItemAdmin(get_registry().for_model(Item)))
    assert mask.allows("name") is True
    assert mask.allows("notes") is False
    assert mask.allows("secret_token") is False
    # Declared relations are an explicit recording opt-in and stay visible.
    assert mask.allows("tags") is True


def test_interface_hidden_fields_stay_hidden_for_superusers(history_env: None) -> None:
    mask = diff_mask(
        Item,
        user=User(is_superuser=True),
        admin=RestrictedItemAdmin(get_registry().for_model(Item)),
    )
    assert mask.allows("notes") is False


def test_excluded_fields_are_interface_hidden_for_everyone(history_env: None) -> None:
    """A field the admin excludes is still tracked, so it stays display-hidden."""
    for user in (User(), User(is_superuser=True)):
        mask = diff_mask(Item, user=user, admin=ExcludedItemAdmin(get_registry().for_model(Item)))
        assert mask.allows("notes") is False


def test_secrets_never_leak_to_anyone(history_env: None) -> None:
    for user in (User(), User(is_superuser=True)):
        mask = diff_mask(Item, user=user, admin=RestrictedItemAdmin(get_registry().for_model(Item)))
        assert mask.allows("secret_token") is False


def test_bespoke_pages_show_every_recorded_field(history_env: None) -> None:
    mask = diff_mask(Item, user=User())
    assert mask.allows("notes") is True
    assert mask.allows("secret_token") is False
    assert mask.allows("never_recorded") is False


async def test_entry_view_filters_diffs_relations_redacted_and_unknown(
    db: AsyncSession,
) -> None:
    item = Item(name="Anvil", sku="ANV-1")
    db.add(item)
    await db.commit()
    item.quantity = 5
    await db.commit()
    [entry] = await history_entries(db, Item, item.id, limit=1)

    mask = DiffMask(
        visible=frozenset({"name"}), known=frozenset({"name", "quantity"}), is_superuser=True
    )
    view = entry_view(entry, mask=mask)
    assert view["diffs"] == []

    unfiltered = entry_view(entry)
    assert [diff["label"] for diff in unfiltered["diffs"]] == ["Quantity"]


async def test_entry_view_maps_redacted_names_to_labels(db: AsyncSession) -> None:
    item = Item(name="Anvil", sku="ANV-1", secret_token="s3cr3t")
    db.add(item)
    await db.commit()
    item.secret_token = "rotated"
    await db.commit()
    [entry] = await history_entries(db, Item, item.id, limit=1)

    assert entry.redacted == ("secret_token",)
    mask = diff_mask(Item, user=User(is_superuser=True))
    assert entry_view(entry, mask=mask)["redacted"] == []


async def test_unpublished_legacy_field_stays_hidden_from_all_viewers(
    db: AsyncSession,
) -> None:
    row = RowChange(
        change_set_id=uuid.uuid4(),
        op="update",
        model_label="warehouse.item",
        object_pk="1",
        object_repr="Anvil",
        changes={
            "fields": {"phone": ["111", "222"]},
            "m2m": {},
            "redacted": [],
            "truncated": {},
            "unknown_old": [],
        },
    )
    db.add(row)
    await db.commit()
    [entry] = await history_entries(db, Item, 1, limit=1)

    root = diff_mask(Item, user=User(is_superuser=True))
    assert entry_view(entry, mask=root)["diffs"] == []

    anonymous = diff_mask(Item, user=User())
    assert entry_view(entry, mask=anonymous)["diffs"] == []


@pytest.mark.parametrize("name", ["tags"])
def test_relation_names_are_known(name: str, history_env: None) -> None:
    mask = diff_mask(Item, user=User())
    assert name in mask.known
