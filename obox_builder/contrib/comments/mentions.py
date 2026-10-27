"""Mention tokens: ``@[u:<id>]`` in comment bodies.

Plain ``@name`` text is never parsed (ADR-0002 D6), which avoids accidental
notifications and username enumeration. Tokens are resolved to real users and
mirrored into ``obox_comment_mention`` at write time.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.contrib.auth.models import Permission, User, role_permissions, user_roles
from obox_builder.contrib.auth.permissions import get_users_permissions
from obox_builder.contrib.comments import COMMENTS_VIEW_PERMISSION
from obox_builder.contrib.comments.exceptions import CommentValidationError

MENTION_RE = re.compile(r"@\[u:(\d{1,18})\]")
MIN_QUERY_LENGTH = 2
DEFAULT_LIMIT = 8
MAX_LIMIT = 20


@dataclass(frozen=True)
class MentionCandidate:
    id: int
    label: str


def parse_mentions(body: str) -> tuple[int, ...]:
    """Distinct mentioned user ids in first-appearance order."""
    seen: set[int] = set()
    result: list[int] = []
    for match in MENTION_RE.finditer(body):
        user_id = int(match.group(1))
        if user_id not in seen:
            seen.add(user_id)
            result.append(user_id)
    return tuple(result)


def display_name(user: User) -> str:
    return (user.full_name or user.username or f"user #{user.id}")[:150]


async def resolve_mentions(
    db: AsyncSession,
    user_ids: Iterable[int],
    *,
    model_view_permission: str,
) -> dict[int, User]:
    """Load and authorise every mentioned user, or raise a field error.

    All users must exist, be active, and hold both the target model's ``view``
    permission and ``comments.view_row_comment``. Nothing is silently dropped:
    a comment that names an unauthorised user fails with a 422-style error.
    """
    ids = {int(user_id) for user_id in user_ids}
    if not ids:
        return {}
    users = {
        int(user.id): user
        for user in (await db.scalars(sa.select(User).where(User.id.in_(ids)))).all()
    }
    missing = ids - set(users)
    if missing:
        missing_names = ", ".join(f"#{user_id}" for user_id in sorted(missing))
        raise CommentValidationError(f"Mentioned user(s) not found: {missing_names}.", field="body")
    for user in users.values():
        if not user.is_active:
            raise CommentValidationError(
                f"{user.username} is inactive and cannot be mentioned.", field="body"
            )
    permissions = await get_users_permissions(db, ids)
    denied = [
        user.username
        for user in users.values()
        if not user.is_superuser
        and not {model_view_permission, COMMENTS_VIEW_PERMISSION} <= permissions[int(user.id)]
    ]
    if denied:
        raise CommentValidationError(
            "These users cannot view this row and cannot be mentioned: "
            + ", ".join(sorted(denied)),
            field="body",
        )
    return users


async def mentionable_users(
    db: AsyncSession,
    query: str,
    *,
    model_view_permission: str,
    limit: int = DEFAULT_LIMIT,
) -> list[MentionCandidate]:
    """Users holding both view permissions, filtered by username/full name."""
    query = query.strip()
    if len(query) < MIN_QUERY_LENGTH:
        return []
    pattern = "%" + _escape_like(query) + "%"
    required = _permission_ids(model_view_permission, COMMENTS_VIEW_PERMISSION)
    eligible = (
        sa.select(user_roles.c.user_id)
        .join(role_permissions, role_permissions.c.role_id == user_roles.c.role_id)
        .where(role_permissions.c.permission_id.in_(required))
        .group_by(user_roles.c.user_id)
        .having(sa.func.count(sa.distinct(role_permissions.c.permission_id)) == 2)
    )
    statement = (
        sa.select(User)
        .where(
            User.is_active.is_(True),
            sa.or_(
                User.is_superuser.is_(True),
                User.id.in_(eligible),
            ),
            sa.or_(
                User.username.ilike(pattern, escape="\\"),
                User.full_name.ilike(pattern, escape="\\"),
            ),
        )
        .order_by(User.username)
        .limit(max(1, min(limit, MAX_LIMIT)))
    )
    users = (await db.scalars(statement)).all()
    return [MentionCandidate(id=int(user.id), label=display_name(user)) for user in users]


def _permission_ids(*permissions: str) -> sa.Select[tuple[int]]:
    clauses = []
    for permission in permissions:
        app_label, _, codename = permission.partition(".")
        clauses.append(sa.and_(Permission.app_label == app_label, Permission.codename == codename))
    return sa.select(Permission.id).where(sa.or_(*clauses))


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


__all__ = [
    "MENTION_RE",
    "MentionCandidate",
    "display_name",
    "mentionable_users",
    "parse_mentions",
    "resolve_mentions",
]
