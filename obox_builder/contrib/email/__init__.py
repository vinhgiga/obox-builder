"""Async email sending with a configurable delivery backend."""

from obox_builder.contrib.email.backends import EmailMessage
from obox_builder.contrib.email.services import send_mail

__all__ = ["EmailMessage", "send_mail"]
