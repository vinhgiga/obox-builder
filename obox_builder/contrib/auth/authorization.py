"""Superuser-only role changes in the caller's unit of work."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.orm import selectinload

from obox_builder.auth import _valid_permission
from obox_builder.context import Context
from obox_builder.errors import (
    N_,
    BuilderValidationError,
    ImproperlyConfigured,
    NotAuthenticated,
    NotFound,
    PermissionDenied,
)

if TYPE_CHECKING:
    from obox_builder.application import Obox
    from obox_builder.contrib.auth import Auth


class Authorization:
    def __init__(self, app: Obox, auth: Auth) -> None:
        self.app, self.auth = app, auth

    def _models(self, ctx: Context) -> tuple[Any, Any]:
        if (
            ctx._owner is not self.app
            or not ctx._active
            or ctx._failed
            or ctx.session.sync_session.get_transaction() is not ctx._transaction
        ):
            raise ImproperlyConfigured("Use an active unit of work from this application.")
        actor = ctx.principal
        if (
            getattr(actor, "is_authenticated", False) is not True
            or getattr(actor, "is_active", False) is not True
        ):
            raise NotAuthenticated(N_("Authentication is required."))
        if (
            getattr(actor, "is_superuser", False) is not True
            or getattr(actor, "api_scopes", None) is not None
        ):
            raise PermissionDenied(N_("Superuser access is required."))
        user_model = getattr(self.auth.backend, "user_model", None)
        mapper = sa.inspect(user_model, raiseerr=False)
        relation = mapper.relationships.get("roles") if mapper is not None else None
        if relation is None or relation.secondary is None:
            raise ImproperlyConfigured("This backend does not provide model-backed roles.")
        return user_model, relation.mapper.class_

    def _permissions(self, values: Sequence[str], *, existing: Sequence[str] = ()) -> list[str]:
        known = self.app.permissions.keys() | set(existing)
        if isinstance(values, str | bytes) or any(
            not isinstance(value, str) or not _valid_permission(value) or value not in known
            for value in values
        ):
            raise BuilderValidationError({"permissions": [N_("Choose valid permissions.")]})
        return sorted(set(values))

    def _audit(
        self,
        ctx: Context,
        resource: str,
        id: Any,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
    ) -> None:
        if self.app.get_plugin("activity") is None:
            return
        from obox_builder.contrib.activity.models import Event

        changes = [
            {"field": name, "kind": "text", "before": before.get(name), "after": after.get(name)}
            for name in dict.fromkeys((*before, *after))
            if before.get(name) != after.get(name)
        ]
        if changes:
            ctx.session.add(
                Event(
                    resource_key=resource,
                    record_id=str(id),
                    actor_id=str(ctx.principal.id),
                    kind="revision",
                    action="create" if not before else "update" if after else "delete",
                    source=ctx.source,
                    changes=changes,
                )
            )

    async def create_role(self, name: str, *, permissions: Sequence[str] = (), ctx: Context) -> Any:
        _, model = self._models(ctx)
        return await self._save_role(model(), name, permissions, ctx)

    async def _save_role(
        self, role: Any, name: str, permissions: Sequence[str], ctx: Context
    ) -> Any:
        name = name.strip()
        if not name:
            raise BuilderValidationError({"name": [N_("This field is required.")]})
        if len(name) > 150:
            raise BuilderValidationError({"name": [N_("Use at most 150 characters.")]})
        before = (
            {"name": role.name, "permissions": list(role.permissions)}
            if role.id is not None
            else {}
        )
        selected = self._permissions(permissions, existing=before.get("permissions", ()))
        model = type(role)
        duplicate = sa.select(model.id).where(model.name == name)
        if role.id is not None:
            duplicate = duplicate.where(model.id != role.id)
        if await ctx.session.scalar(duplicate) is not None:
            raise BuilderValidationError({"name": [N_("This role name is already taken.")]})
        role.name, role.permissions = name, selected
        ctx.session.add(role)
        await ctx.session.flush()
        self._audit(ctx, "auth.roles", role.id, before, {"name": name, "permissions": selected})
        return role

    async def update_role(
        self,
        id: Any,
        *,
        name: str,
        permissions: Sequence[str],
        ctx: Context,
    ) -> Any:
        _, model = self._models(ctx)
        role = await ctx.session.get(model, id)
        if role is None:
            raise NotFound(N_("Role not found."))
        return await self._save_role(role, name, permissions, ctx)

    async def change_permissions(
        self, id: Any, permissions: Sequence[str], *, grant: bool, ctx: Context
    ) -> None:
        _, model = self._models(ctx)
        role = await ctx.session.get(model, id)
        if role is None:
            raise NotFound(N_("Role not found."))
        selected = self._permissions(permissions, existing=() if grant else role.permissions)
        values = set(role.permissions)
        updated = values.union(selected) if grant else values.difference(selected)
        if updated != values:
            await self._save_role(role, role.name, sorted(updated), ctx)

    async def change_roles(
        self, id: Any, role_ids: Sequence[Any], *, add: bool, ctx: Context
    ) -> None:
        user_model, role_model = self._models(ctx)
        user = await ctx.session.scalar(
            sa.select(user_model)
            .where(user_model.id == id)
            .options(selectinload(user_model.roles))
            .with_for_update()
        )
        if user is None:
            raise NotFound(N_("User not found."))
        requested = set(role_ids)
        roles = list(
            await ctx.session.scalars(sa.select(role_model).where(role_model.id.in_(requested)))
        )
        if {role.id for role in roles} != requested:
            raise BuilderValidationError({"role_ids": [N_("Choose valid roles.")]})
        current = {role.id: role for role in user.roles}
        if add:
            current.update((role.id, role) for role in roles)
        else:
            current = {key: role for key, role in current.items() if key not in requested}
        await self.set_roles(user, list(current.values()), ctx=ctx)

    async def set_roles(self, user: Any, roles: Sequence[Any], *, ctx: Context) -> None:
        user_model, role_model = self._models(ctx)
        if not isinstance(user, user_model) or any(
            not isinstance(role, role_model) for role in roles
        ):
            raise ImproperlyConfigured("Assign roles from this authentication backend.")
        before = sorted(str(role.id) for role in user.roles)
        user.roles = list({role.id: role for role in roles}.values())
        await ctx.session.flush()
        self._audit(
            ctx,
            "auth.users",
            user.id,
            {"roles": before},
            {"roles": sorted(str(role.id) for role in user.roles)},
        )

    async def delete_role(self, id: Any, *, ctx: Context) -> None:
        user_model, role_model = self._models(ctx)
        role = await ctx.session.get(role_model, id)
        if role is None:
            raise NotFound(N_("Role not found."))
        relation = sa.inspect(user_model).relationships["roles"]
        primary_key = sa.inspect(role_model).primary_key[0]
        column = next(
            column
            for column in relation.secondary.columns
            if any(fk.column is primary_key for fk in column.foreign_keys)
        )
        await ctx.session.execute(sa.delete(relation.secondary).where(column == role.id))
        self._audit(
            ctx,
            "auth.roles",
            role.id,
            {"name": role.name, "permissions": list(role.permissions)},
            {},
        )
        await ctx.session.delete(role)
        await ctx.session.flush()
