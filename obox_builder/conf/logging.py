from __future__ import annotations

import logging.config
from typing import Any

DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def default_logging_config(*, debug: bool = False) -> dict[str, Any]:
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"format": DEFAULT_LOG_FORMAT},
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": "INFO",
                "formatter": "default",
            },
        },
        "loggers": {
            "obox": {"handlers": ["console"], "level": "INFO"},
            "obox_builder": {
                "handlers": ["console"],
                "level": "INFO" if debug else "WARNING",
            },
            "alembic": {"level": "WARNING"},
        },
    }


def configure_logging(*, logging_config: dict[str, Any] | None, debug: bool = False) -> None:
    logging.config.dictConfig(logging_config or default_logging_config(debug=debug))
