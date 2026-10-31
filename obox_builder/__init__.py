"""Model-driven CRUD applications built on native FastAPI and SQLAlchemy."""

__version__ = "0.4.0"

from obox_builder import builder
from obox_builder.application import Diagnostic, Obox, Registration
from obox_builder.auth import Policy, Principal
from obox_builder.builder import API, Form, ModelBuilder, View
from obox_builder.builder.service import ResourceService
from obox_builder.conf import Settings
from obox_builder.context import Context, DomainEvent
from obox_builder.errors import BuilderValidationError, ImproperlyConfigured

__all__ = [
    "API",
    "BuilderValidationError",
    "Context",
    "DomainEvent",
    "Diagnostic",
    "Form",
    "ImproperlyConfigured",
    "ModelBuilder",
    "Obox",
    "Policy",
    "Principal",
    "Registration",
    "ResourceService",
    "Settings",
    "View",
    "__version__",
    "builder",
]
