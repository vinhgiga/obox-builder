"""Safe rendering for plain-text comment bodies.

One linear pass over the input produces text, URL and mention nodes; each node
is escaped on its own. The result is a :class:`markupsafe.Markup`, and templates
never apply the ``safe`` filter to comment data (ADR-0002 section 6).
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from markupsafe import Markup, escape

TOKEN_RE = re.compile(r"@\[u:(\d{1,18})\]|(?P<url>(?i:https?://|mailto:)[^\s<>\"']+)")
URL_TRAILING = ".,;:!?"


def render_body(body: str | None, names: Mapping[int, str] | None = None) -> Markup:
    """Render ``body`` to safe HTML. Unknown mention ids stay neutral placeholders."""
    if not body:
        return Markup("")
    known = names or {}
    nodes: list[Markup] = []
    position = 0
    for match in TOKEN_RE.finditer(body):
        nodes.append(_text(body[position : match.start()]))
        raw_user_id = match.group(1)
        if raw_user_id is not None:
            user_id = int(raw_user_id)
            label = known.get(user_id)
            if label:
                nodes.append(Markup('<span class="comment-mention">@{0}</span>').format(label))
            else:
                nodes.append(Markup('<span class="comment-mention">@unknown</span>'))
        else:
            url = match.group("url")
            trimmed = url.rstrip(URL_TRAILING)
            trailing = url[len(trimmed) :]
            if trimmed:
                nodes.append(
                    Markup(
                        '<a href="{0}" target="_blank" '
                        'rel="noopener noreferrer nofollow ugc">{0}</a>'
                    ).format(trimmed)
                )
            if trailing:
                nodes.append(_text(trailing))
        position = match.end()
    nodes.append(_text(body[position:]))
    return Markup("").join(nodes)


def _text(value: str) -> Markup:
    if not value:
        return Markup("")
    return Markup("<br>\n").join(escape(part) for part in value.split("\n"))


__all__ = ["render_body"]
