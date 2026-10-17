"""HTTP layer: rendering shortcuts, flash messages and URL helpers."""

from obox_builder.http.messages import add_message, clear_messages, get_and_clear_messages
from obox_builder.http.shortcuts import redirect, render, render_template
from obox_builder.http.urls import is_safe_url, safe_next_url

__all__ = [
    "add_message",
    "clear_messages",
    "get_and_clear_messages",
    "is_safe_url",
    "redirect",
    "render",
    "render_template",
    "safe_next_url",
]
