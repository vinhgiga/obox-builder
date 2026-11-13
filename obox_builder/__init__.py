"""Model-driven CRUD applications built on native FastAPI and SQLAlchemy."""

__version__ = "0.4.0"

from obox_builder import builder
from obox_builder.application import Diagnostic, Obox, Registration
from obox_builder.apps import AppConfig
from obox_builder.auth import Policy, Principal
from obox_builder.builder import API, Form, Inline, ModelBuilder, View
from obox_builder.builder.service import BatchOperation, ResourceService
from obox_builder.conf import Settings
from obox_builder.context import Context, DomainEvent
from obox_builder.errors import BuilderValidationError, ImproperlyConfigured
from obox_builder.plugin import Plugin

__all__ = [
    "API",
    "AppConfig",
    "BatchOperation",
    "BuilderValidationError",
    "Context",
    "Diagnostic",
    "DomainEvent",
    "Form",
    "ImproperlyConfigured",
    "Inline",
    "ModelBuilder",
    "Obox",
    "Plugin",
    "Policy",
    "Principal",
    "Registration",
    "ResourceService",
    "Settings",
    "View",
    "__version__",
    "builder",
]
