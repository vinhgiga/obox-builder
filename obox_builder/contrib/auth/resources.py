"""Explicit account contracts; write workflows retain the account administration policy."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import selectinload

from obox_builder.contrib.auth import administration, get_user_model, services, sessions
from obox_builder.contrib.auth.models import ApiToken, Permission, Role
from obox_builder.contrib.auth.schemas import RoleCreate, RoleInput, UserInput, UserPatch
from obox_builder.contrib.auth.validators import PasswordValidationError
from obox_builder.core.utils.time import utcnow
from obox_builder.resources import (
    OperationContext,
    ReadOnlyModelResource,
    ResourcePolicy,
    ResourceValidationError,
    capability,
    operation,
    register,
)
from obox_builder.resources.models import _builtin


def identifiers(values: list[str]) -> list[int]:
    if any(
        not value.isascii()
        or not value.isdecimal()
        or len(value) > 19
        or not 0 < int(value) < 2**63
        for value in values
    ):
        raise ResourceValidationError({"__all__": ["IDs must be positive decimal strings."]})
    return list(dict.fromkeys(map(int, values)))


async def account_roles(ctx, values):
    try:
        return await administration.selected_roles(
            ctx.session, ctx.delegation_actor(), identifiers(values)
        )
    except ValueError as exc:
        raise ResourceValidationError({"role_ids": [str(exc)]}) from exc


class HeldPermissions(ResourcePolicy):
    def scope(self, ctx, query, operation):
        if ctx.actor.is_superuser and ctx.scopes is None:
            return query
        name = Permission.service + "." + Permission.resource + "." + Permission.action
        return query.where(name.in_(ctx.actor.permissions))


class OwnedTokens(ResourcePolicy):
    def scope(self, ctx, query, operation):
        return query.where(ApiToken.user_id == ctx.actor.id)


@register()
class UserResource(ReadOnlyModelResource):
    name = "auth.users"
    label = "User"
    manage = capability("Can manage users", effect="write")

    def __init__(self):
        self.model = get_user_model()
        self.name = self.model._model_label.replace("_", "") + ".users"
        names = (
            "id",
            self.model.USERNAME_FIELD,
            self.model.EMAIL_FIELD,
            "full_name",
            "is_active",
            "is_superuser",
            "last_login_at",
            "password_changed_at",
            "created_at",
            "updated_at",
            "version",
            "roles",
        )
        mapper = sa.inspect(self.model)
        self.fields = tuple(dict.fromkeys(name for name in names if name in mapper.attrs))
        self.ordering = (self.model.USERNAME_FIELD,)
        self.search_fields = tuple(
            dict.fromkeys(
                name
                for name in (self.model.USERNAME_FIELD, self.model.EMAIL_FIELD)
                if name in self.fields
            )
        )
        super().__init__()

    async def serialize(self, ctx, obj):
        result = await super().serialize(ctx, obj)
        data = result.model_dump(exclude_unset=True)
        if "roles" in result.model_fields_set:
            data["roles"] = sorted(str(role.id) for role in obj.roles)
        return self.read_schema.model_validate(data)

    def get_queryset(self, ctx):
        return (
            super()
            .get_queryset(ctx)
            .options(selectinload(self.model.roles).selectinload(Role.permissions))
        )

    async def resolve_target(self, ctx, spec, target):
        if spec.effect != "read":
            await administration.lock_superusers(ctx.session)
        return await super().resolve_target(ctx, spec, target)

    def builtin_schemas(self, method):
        if method in {"create", "update"}:
            return (UserInput if method == "create" else UserPatch, self.read_schema)
        return super().builtin_schemas(method)

    @_builtin(scope="collection", permission="manage", label="Can manage users")
    async def create(self, ctx: OperationContext, data: UserInput):
        await administration.check_user(
            ctx.session,
            ctx.delegation_actor(),
            None,
            active=data.is_active,
            superuser=data.is_superuser,
        )
        roles = await account_roles(ctx, data.role_ids)
        async with ctx.session.begin_nested():
            try:
                target = await services.create_user(
                    ctx.session,
                    data.username,
                    data.password,
                    email=data.email,
                    full_name=data.full_name,
                    is_active=data.is_active,
                    is_superuser=data.is_superuser,
                )
            except (ValueError, PasswordValidationError) as exc:
                raise ResourceValidationError({"__all__": [str(exc)]}) from exc
            await ctx.session.refresh(target, ["roles"])
            target.roles = roles
            await ctx.session.flush()
            return await self.serialize(ctx, target)

    @_builtin(scope="record", permission="manage", label="Can manage users")
    async def update(self, ctx: OperationContext, obj, data: UserPatch):
        values = data.model_dump(exclude_unset=True)
        await administration.check_user(
            ctx.session,
            ctx.delegation_actor(),
            obj,
            active=values.get("is_active", obj.is_active),
            superuser=values.get("is_superuser", obj.is_superuser),
        )
        async with ctx.session.begin_nested():
            if "role_ids" in values:
                obj.roles = await account_roles(ctx, values.pop("role_ids"))
            password = values.pop("password", None)
            for name, value in values.items():
                attr = self.model.EMAIL_FIELD if name == "email" else name
                if attr not in sa.inspect(self.model).attrs:
                    if value:
                        raise ResourceValidationError({name: ["This user field is unavailable."]})
                    continue
                setattr(obj, attr, services.normalize_email(value) if name == "email" else value)
            if password:
                try:
                    await services.set_password(ctx.session, obj, password)
                except PasswordValidationError as exc:
                    raise ResourceValidationError({"password": list(exc.errors)}) from exc
            if not obj.is_active:
                await sessions.revoke_user_sessions(ctx.session, obj.id)
            obj.updated_at = utcnow()
            await ctx.session.flush()
            return await self.serialize(ctx, obj)

    @_builtin(scope="record", effect="delete", permission="manage", label="Can manage users")
    async def delete(self, ctx: OperationContext, obj) -> None:
        await administration.check_user(
            ctx.session, ctx.delegation_actor(), obj, active=False, superuser=False
        )
        await ctx.session.delete(obj)
        await ctx.session.flush()


@register()
class RoleResource(ReadOnlyModelResource[Role]):
    name = "auth.roles"
    label = "Role"
    model = Role
    fields = ("id", "name", "permissions", "created_at", "updated_at", "version")
    manage = capability("Can manage roles", effect="write")

    async def serialize(self, ctx, obj):
        result = await super().serialize(ctx, obj)
        data = result.model_dump(exclude_unset=True)
        if "permissions" in result.model_fields_set:
            data["permissions"] = [str(permission.id) for permission in obj.permissions]
        return self.read_schema.model_validate(data)

    def builtin_schemas(self, method):
        if method in {"create", "update"}:
            return (RoleCreate if method == "create" else RoleInput, self.read_schema)
        return super().builtin_schemas(method)

    async def bind_role(self, ctx, data, obj=None):
        actor = ctx.delegation_actor()
        if obj is not None:
            administration.check_role(actor, obj)
        values = data.model_dump(exclude_unset=True)
        if "name" in values:
            values["name"] = values["name"].strip()
            duplicate = sa.select(Role.id).where(Role.name == values["name"])
            if obj is not None:
                duplicate = duplicate.where(Role.id != obj.id)
            if not values["name"]:
                raise ResourceValidationError({"name": ["A role name is required."]})
            if await ctx.session.scalar(duplicate):
                raise ResourceValidationError(
                    {"name": [f"A role named {values['name']!r} already exists."]}
                )
        if "permission_ids" in values:
            ids = identifiers(values.pop("permission_ids"))
            permissions = list(
                await ctx.session.scalars(sa.select(Permission).where(Permission.id.in_(ids)))
            )
            if {permission.id for permission in permissions} != set(ids):
                raise ResourceValidationError({"permission_ids": ["Unknown permission."]})
            administration.check_permissions(
                actor, permissions, existing=obj.permissions if obj is not None else ()
            )
            values["permissions"] = permissions
        return values

    @_builtin(scope="collection", permission="manage", label="Can manage roles")
    async def create(self, ctx: OperationContext, data: RoleCreate):
        obj = Role(**await self.bind_role(ctx, data))
        ctx.session.add(obj)
        await ctx.session.flush()
        await ctx.session.refresh(obj, ["permissions"])
        return await self.serialize(ctx, obj)

    @_builtin(scope="record", permission="manage", label="Can manage roles")
    async def update(self, ctx: OperationContext, obj: Role, data: RoleInput):
        for name, value in (await self.bind_role(ctx, data, obj)).items():
            setattr(obj, name, value)
        await ctx.session.flush()
        return await self.serialize(ctx, obj)

    @_builtin(scope="record", effect="delete", permission="manage", label="Can manage roles")
    async def delete(self, ctx: OperationContext, obj: Role) -> None:
        administration.check_role(ctx.delegation_actor(), obj)
        await ctx.session.delete(obj)
        await ctx.session.flush()


@register()
class PermissionResource(ReadOnlyModelResource[Permission]):
    name = "auth.permission"
    label = "Permission"
    model = Permission
    fields = ("id", "service", "resource", "action")
    policy = HeldPermissions()


@register()
class ApiTokenResource(ReadOnlyModelResource[ApiToken]):
    name = "auth.apiToken"
    label = "API token"
    model = ApiToken
    fields = (
        "id",
        "user_id",
        "name",
        "token_prefix",
        "scopes",
        "expires_at",
        "revoked_at",
        "last_used_at",
        "last_used_ip",
        "created_at",
        "updated_at",
    )
    policy = OwnedTokens()

    @operation(scope="selection", permission="update", label="Can update API token")
    async def revoke(self, ctx: OperationContext, objects: list[ApiToken]) -> None:
        for token in objects:
            if token.revoked_at is None:
                token.revoked_at = utcnow()
        await ctx.session.flush()
