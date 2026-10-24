from __future__ import annotations

from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from obox_builder.contrib.comments.models import CommentMention, RowComment
from obox_builder.db.session import session_scope
from tests.comments.conftest import (
    csrf_for,
    grant_permissions,
    login_session,
    make_user,
)
from tests.support.apps.commentsdemo.models import Ticket

pytestmark = pytest.mark.usefixtures("comments_env")

BASE = "/admin/commentsdemo/ticket"


async def make_ticket(title: str = "Printer is on fire") -> int:
    async with session_scope() as db:
        ticket = Ticket(title=title)
        db.add(ticket)
        await db.flush()
        return ticket.id


async def comments_of(ticket_id: int) -> list[RowComment]:
    async with session_scope() as db:
        return list(
            (
                await db.scalars(
                    sa.select(RowComment)
                    .where(
                        RowComment.model_label == "commentsdemo.ticket",
                        RowComment.object_pk == str(ticket_id),
                    )
                    .order_by(RowComment.id)
                )
            ).all()
        )


async def post_comment(
    client: httpx.AsyncClient,
    ticket_id: int,
    token: str,
    body: str,
    **extra: Any,
) -> httpx.Response:
    return await client.post(
        f"{BASE}/{ticket_id}/comments",
        data={"csrf_token": token, "body": body, **extra},
        follow_redirects=False,
    )


async def logged_in_superuser(client: httpx.AsyncClient) -> str:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    return await csrf_for("alice")


async def test_comments_cannot_bypass_model_queryset(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from obox_builder.contrib.admin import ModelAdmin
    from obox_builder.contrib.admin.site import site

    pk = await make_ticket("Scoped ticket")
    token = await logged_in_superuser(client)
    assert (await post_comment(client, pk, token, "Private body")).status_code == 303
    admin = site.get_model_admin_for(Ticket)
    assert admin is not None
    monkeypatch.setattr(
        admin,
        "get_queryset",
        lambda ctx: ModelAdmin.get_queryset(admin, ctx).where(Ticket.id != pk),
    )
    response = await client.get(f"{BASE}/{pk}/comments")
    assert response.status_code == 404
    assert "Private body" not in response.text
    assert (await post_comment(client, pk, token, "Forged")).status_code == 404
    assert len(await comments_of(pk)) == 1


async def test_comments_page_and_composer_render(client: httpx.AsyncClient) -> None:
    ticket_id = await make_ticket()
    await logged_in_superuser(client)
    response = await client.get(f"{BASE}/{ticket_id}/comments")
    assert response.status_code == 200
    assert "Ticket comments" in response.text
    assert "Add a comment" in response.text
    assert "No comments yet." in response.text


async def test_post_comment_renders_escaped_body_and_links(
    client: httpx.AsyncClient,
) -> None:
    ticket_id = await make_ticket()
    token = await logged_in_superuser(client)
    response = await post_comment(
        client,
        ticket_id,
        token,
        '<script>alert("x")</script> see https://example.com',
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/comments#comment-1")
    page = await client.get(f"{BASE}/{ticket_id}/comments")
    assert page.status_code == 200
    assert "<script>alert" not in page.text
    assert "&lt;script&gt;alert" in page.text
    assert '<a href="https://example.com"' in page.text


async def test_reply_edit_resolve_and_reopen(client: httpx.AsyncClient) -> None:
    ticket_id = await make_ticket()
    token = await logged_in_superuser(client)
    await post_comment(client, ticket_id, token, "root")
    reply = await post_comment(client, ticket_id, token, "reply", parent_id="1")
    assert reply.status_code == 303

    edit = await client.post(
        f"{BASE}/{ticket_id}/comments/2/edit",
        data={"csrf_token": token, "body": "edited reply"},
        follow_redirects=False,
    )
    assert edit.status_code == 303

    resolve = await client.post(
        f"{BASE}/{ticket_id}/comments/1/resolve",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert resolve.status_code == 303
    page = await client.get(f"{BASE}/{ticket_id}/comments")
    assert "resolved" in page.text
    assert "edited reply" in page.text

    reopen = await client.post(
        f"{BASE}/{ticket_id}/comments/1/reopen",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert reopen.status_code == 303


async def test_delete_is_a_tombstone_and_moderators_see_the_body(
    client: httpx.AsyncClient,
) -> None:
    ticket_id = await make_ticket()
    token = await logged_in_superuser(client)
    await post_comment(client, ticket_id, token, "secret text")
    response = await client.post(
        f"{BASE}/{ticket_id}/comments/1/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    rows = await comments_of(ticket_id)
    assert rows[0].deleted_at is not None
    assert rows[0].body == "secret text"
    page = await client.get(f"{BASE}/{ticket_id}/comments")
    assert "deleted" in page.text
    assert "secret text" in page.text


async def test_csrf_is_required(client: httpx.AsyncClient) -> None:
    ticket_id = await make_ticket()
    await logged_in_superuser(client)
    response = await client.post(
        f"{BASE}/{ticket_id}/comments", data={"body": "no token"}, follow_redirects=False
    )
    assert response.status_code == 403


async def test_invalid_and_too_long_bodies_return_422(client: httpx.AsyncClient) -> None:
    ticket_id = await make_ticket()
    token = await logged_in_superuser(client)
    empty = await post_comment(client, ticket_id, token, "   ")
    assert empty.status_code == 422
    assert "Write something" in empty.text
    too_long = await post_comment(client, ticket_id, token, "x" * 10_001)
    assert too_long.status_code == 422
    assert "too long" in too_long.text


async def test_read_only_user_can_view_but_not_post(client: httpx.AsyncClient) -> None:
    ticket_id = await make_ticket()
    await make_user("viewer")
    await grant_permissions("viewer", "commentsdemo.view_ticket", "comments.view_row_comment")
    await login_session(client, "viewer")
    page = await client.get(f"{BASE}/{ticket_id}/comments")
    assert page.status_code == 200
    assert "Add a comment" not in page.text
    assert "do not have permission to post" in page.text
    token = await csrf_for("viewer")
    denied = await post_comment(client, ticket_id, token, "nope")
    assert denied.status_code == 403


async def test_user_without_comment_permission_is_denied(client: httpx.AsyncClient) -> None:
    ticket_id = await make_ticket()
    await make_user("viewer")
    await grant_permissions("viewer", "commentsdemo.view_ticket")
    await login_session(client, "viewer")
    assert (await client.get(f"{BASE}/{ticket_id}/comments")).status_code == 403


async def test_replies_disabled_is_enforced_server_side(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    token = await csrf_for("alice")
    async with session_scope() as db:
        from tests.support.apps.commentsdemo.models import Article

        article = Article(title="No replies")
        db.add(article)
        await db.flush()
        article_id = article.id
    first = await client.post(
        f"/admin/commentsdemo/article/{article_id}/comments",
        data={"csrf_token": token, "body": "root"},
        follow_redirects=False,
    )
    assert first.status_code == 303
    reply = await client.post(
        f"/admin/commentsdemo/article/{article_id}/comments",
        data={"csrf_token": token, "body": "reply", "parent_id": "1"},
        follow_redirects=False,
    )
    assert reply.status_code == 422
    page = await client.get(f"/admin/commentsdemo/article/{article_id}/comments")
    assert "Reply" not in page.text


async def test_deleted_row_keeps_a_read_only_comment_page(client: httpx.AsyncClient) -> None:
    ticket_id = await make_ticket()
    token = await logged_in_superuser(client)
    await post_comment(client, ticket_id, token, "kept for audit")
    async with session_scope() as db:
        ticket = await db.get(Ticket, ticket_id)
        assert ticket is not None
        await db.delete(ticket)
    page = await client.get(f"{BASE}/{ticket_id}/comments")
    assert page.status_code == 200
    assert "has been deleted" in page.text
    assert "Add a comment" not in page.text
    denied = await post_comment(client, ticket_id, token, "late")
    assert denied.status_code == 404


async def test_list_page_shows_a_comment_badge_and_context_url(
    client: httpx.AsyncClient,
) -> None:
    ticket_id = await make_ticket()
    token = await logged_in_superuser(client)
    await post_comment(client, ticket_id, token, "one")
    await post_comment(client, ticket_id, token, "two", parent_id="1")
    page = await client.get(BASE)
    assert page.status_code == 200
    assert f'data-comments-url="{BASE}/{ticket_id}/comments"' in page.text
    assert "Comments" in page.text


async def test_form_card_lists_the_latest_comments(client: httpx.AsyncClient) -> None:
    ticket_id = await make_ticket()
    token = await logged_in_superuser(client)
    await post_comment(client, ticket_id, token, "card body")
    page = await client.get(f"{BASE}/{ticket_id}")
    assert page.status_code == 200
    assert "Open comments" in page.text
    assert "card body" in page.text


async def test_mention_autocomplete_and_tokens(client: httpx.AsyncClient) -> None:
    ticket_id = await make_ticket()
    token = await logged_in_superuser(client)
    bob_id = await make_user("bob")
    await grant_permissions("bob", "commentsdemo.view_ticket", "comments.view_row_comment")
    results = await client.get(
        "/admin/comments/mentionable",
        params={"model": "commentsdemo.ticket", "q": "bo"},
    )
    assert results.status_code == 200
    payload = results.json()
    assert payload["results"] == [{"id": bob_id, "label": "bob"}]

    posted = await post_comment(client, ticket_id, token, f"hi @[u:{bob_id}]")
    assert posted.status_code == 303
    async with session_scope() as db:
        count = await db.scalar(sa.select(sa.func.count()).select_from(CommentMention))
    assert count == 1


async def test_rate_limit_returns_429_with_retry_after(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obox_builder.conf import settings

    ticket_id = await make_ticket()
    token = await logged_in_superuser(client)
    monkeypatch.setattr(settings._wrapped, "COMMENTS_RATE_LIMIT_PER_MINUTE", 1)
    first = await post_comment(client, ticket_id, token, "one")
    assert first.status_code == 303
    second = await post_comment(client, ticket_id, token, "two")
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) >= 1


async def test_mentionable_requires_a_commentable_model(client: httpx.AsyncClient) -> None:
    await logged_in_superuser(client)
    response = await client.get(
        "/admin/comments/mentionable", params={"model": "shop.product", "q": "bo"}
    )
    assert response.status_code == 404
