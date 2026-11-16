from typing import Any

from obox.urls import path


class SimpleRouter:
    def __init__(self, trailing_slash: bool = True) -> None:
        self.trailing_slash = "/" if trailing_slash else ""
        self.registry: list[tuple[str, Any, str]] = []

    def register(self, prefix: str, viewset: Any, basename: str | None = None) -> None:
        if basename is None:
            query = getattr(viewset, "queryset", None)
            if query is None:
                raise AssertionError("Specify basename when a viewset has no queryset.")
            basename = query.column_descriptions[0]["entity"].__name__.lower()
        if any(item[2] == basename for item in self.registry):
            raise ValueError(f"Router basename {basename!r} is already registered.")
        basename = str(basename)
        self.registry.append((prefix, viewset, basename))

    @property
    def urls(self) -> list[Any]:
        patterns = []
        for prefix, cls, basename in self.registry:
            lookup = cls.lookup_url_kwarg or cls.lookup_field
            for detail, actions in (
                (False, {"get": "list", "post": "create"}),
                (
                    True,
                    {
                        "get": "retrieve",
                        "put": "update",
                        "patch": "partial_update",
                        "delete": "destroy",
                    },
                ),
            ):
                mapping = {
                    method: action for method, action in actions.items() if hasattr(cls, action)
                }
                if mapping:
                    route = prefix + (f"/<str:{lookup}>" if detail else "") + self.trailing_slash
                    patterns.append(
                        path(
                            route,
                            cls.as_view(mapping, basename=basename, detail=detail),
                            name=basename + ("-detail" if detail else "-list"),
                        )
                    )
            for name in dir(cls):
                method = getattr(cls, name)
                if hasattr(method, "mapping"):
                    route = (
                        prefix
                        + (f"/<str:{lookup}>" if method.detail else "")
                        + "/"
                        + method.url_path
                        + self.trailing_slash
                    )
                    patterns.append(
                        path(
                            route,
                            cls.as_view(
                                method.mapping,
                                basename=basename,
                                detail=method.detail,
                                **method.kwargs,
                            ),
                            name=f"{basename}-{method.url_name}",
                        )
                    )
        # Collection actions must precede a potentially matching detail lookup.
        return sorted(patterns, key=lambda item: "<" in item.route)
