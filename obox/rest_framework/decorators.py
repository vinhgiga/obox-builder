from typing import Any


def action(
    *,
    methods: Any = None,
    detail: bool | None = None,
    url_path: str | None = None,
    url_name: str | None = None,
    **kwargs: Any,
) -> Any:
    if detail is None:
        raise AssertionError("An action must specify detail.")

    def decorator(method: Any) -> Any:
        method.mapping = {verb.lower(): method.__name__ for verb in methods or ["get"]}
        method.detail = detail
        method.url_path = url_path or method.__name__
        method.url_name = url_name or method.__name__.replace("_", "-")
        method.kwargs = kwargs
        return method

    return decorator
