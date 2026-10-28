from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase, Relationship
from sqlalchemy.orm import relationship as _relationship

from obox_builder.apps import apps
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.utils.strings import camel_to_snake

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class ModelBase(DeclarativeBase):
    """Declarative base carrying the project metadata and naming convention."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Model(ModelBase):
    """Base model: table name defaults to ``obox_<snake_name>``.

    The owning app is found by matching the model's module against the app
    registry (longest ``AppConfig.name`` prefix). Override ``app_label`` on a
    model class when the module cannot be matched. Models that map their own
    ``app_label`` column keep the registry-derived value in ``_model_label``.
    """

    __abstract__ = True

    app_label: ClassVar[str] = ""
    _model_label: ClassVar[str] = ""
    __auth_model__: ClassVar[bool] = False
    __permissions__: ClassVar[list[tuple[str, str]]] = []
    #: Row-history declaration: ``True`` for defaults, ``False``/``None`` to opt
    #: out, or a mapping with ``fields``/``exclude``/``relations``/``mask``/
    #: ``retention_days``.
    __history__: ClassVar[bool | Mapping[str, Any] | None] = None
    #: Row-comments declaration: ``True`` for defaults, ``False``/``None`` to opt
    #: out, or a mapping with ``max_length``/``allow_replies``/
    #: ``edit_window_seconds`` (see ``docs/comments.md``).
    __comments__: ClassVar[bool | Mapping[str, Any] | None] = None
    #: Opt-in durable data-change events.
    __events__: ClassVar[bool | Mapping[str, Any] | None] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        if not cls.__dict__.get("__abstract__", False):
            raw_label = cls.__dict__.get("app_label")
            if isinstance(raw_label, str) and raw_label:
                label = raw_label
            else:
                app_config = apps.get_containing_app_config(cls.__module__)
                if app_config is None:
                    raise ImproperlyConfigured(
                        f"Model {cls.__qualname__} (defined in {cls.__module__!r}) is not "
                        "part of any installed app; add its app to INSTALLED_APPS or set "
                        "app_label on the model."
                    )
                label = app_config.label
                if raw_label is None:
                    cls.app_label = label
            cls._model_label = label
            if "__tablename__" not in cls.__dict__:
                prefix = label if label == "auth" or cls.__auth_model__ else "obox"
                cls.__tablename__ = f"{prefix}_{camel_to_snake(cls.__name__)}"
        super().__init_subclass__(**kwargs)


def relationship(*args: Any, **kwargs: Any) -> Relationship[Any]:
    """``sqlalchemy.relationship`` defaulting to ``lazy="raise"`` (async safety)."""
    kwargs.setdefault("lazy", "raise")
    return _relationship(*args, **kwargs)
