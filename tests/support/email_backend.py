from __future__ import annotations

from collections.abc import Sequence

from obox_builder.contrib.email.backends import EmailMessage

MESSAGES: list[EmailMessage] = []


class MemoryBackend:
    async def send_messages(self, messages: Sequence[EmailMessage]) -> int:
        MESSAGES.extend(messages)
        return len(messages)
