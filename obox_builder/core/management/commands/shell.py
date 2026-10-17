from __future__ import annotations

import code

from obox_builder.core.management import utils
from obox_builder.core.management.base import command


def _build_namespace() -> dict[str, object]:
    from obox_builder.apps import apps
    from obox_builder.conf import settings
    from obox_builder.db import session_scope

    namespace: dict[str, object] = {
        "apps": apps,
        "session_scope": session_scope,
        "settings": settings,
    }
    for model in apps.get_models():
        namespace[model.__name__] = model
    return namespace


@command(help="Run an interactive Python shell with the project loaded.", requires_checks=True)
def handle() -> None:
    namespace = _build_namespace()
    try:
        import IPython  # type: ignore[import-not-found]
    except ImportError:
        code.interact(
            banner="obox-builder shell (session_scope, settings and models are available)",
            local=namespace,
        )
        return
    IPython.start_ipython(argv=[], user_ns=namespace)  # type: ignore[no-untyped-call]
    utils.echo("Shell closed.")
