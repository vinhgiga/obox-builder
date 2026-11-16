"""Small escaped widgets following Django's render(name, value, attrs) contract."""

from __future__ import annotations

from html import escape
from typing import Any

from markupsafe import Markup


def _attrs(values: dict[str, Any]) -> str:
    return " ".join(
        escape(str(key), quote=True)
        if value is True
        else f'{escape(str(key), quote=True)}="{escape(str(value), quote=True)}"'
        for key, value in values.items()
        if value is not False and value is not None
    )


class Widget:
    input_type = "text"

    def __init__(self, attrs: dict[str, Any] | None = None) -> None:
        self.attrs = dict(attrs or {})

    def render(
        self, name: str, value: Any, attrs: dict[str, Any] | None = None, renderer: Any = None
    ) -> Markup:
        attributes = {"type": self.input_type, "name": name, **self.attrs, **(attrs or {})}
        if value not in (None, ""):
            attributes["value"] = value
        return Markup("<input " + _attrs(attributes) + ">")  # noqa: S704


class TextInput(Widget):
    pass


class NumberInput(Widget):
    input_type = "number"


class HiddenInput(Widget):
    input_type = "hidden"


class PasswordInput(Widget):
    input_type = "password"

    def __init__(self, attrs: Any = None, render_value: bool = False) -> None:
        super().__init__(attrs)
        self.render_value = render_value

    def render(self, name: str, value: Any, attrs: Any = None, renderer: Any = None) -> Markup:
        return super().render(name, value if self.render_value else None, attrs, renderer)


class CheckboxInput(Widget):
    input_type = "checkbox"

    def render(self, name: str, value: Any, attrs: Any = None, renderer: Any = None) -> Markup:
        options = dict(attrs or {})
        if value not in (None, False, "", "false", "False", "0"):
            options["checked"] = True
        return super().render(name, None, options, renderer)


class Textarea(Widget):
    def __init__(self, attrs: Any = None) -> None:
        super().__init__({"cols": 40, "rows": 10, **(attrs or {})})

    def render(self, name: str, value: Any, attrs: Any = None, renderer: Any = None) -> Markup:
        attributes = _attrs({"name": name, **self.attrs, **(attrs or {})})
        return Markup(  # noqa: S704 - values and attributes are escaped above.
            f"<textarea {attributes}>"
            + escape(str(value) if value is not None else "")
            + "</textarea>"
        )


class Select(Widget):
    def __init__(self, attrs: Any = None, choices: Any = ()) -> None:
        super().__init__(attrs)
        self.choices = list(choices)

    def render(self, name: str, value: Any, attrs: Any = None, renderer: Any = None) -> Markup:
        options = []
        for key, label in self.choices:
            attributes = _attrs({"value": key, "selected": str(key) == str(value)})
            options.append(f"<option {attributes}>{escape(str(label))}</option>")
        return Markup(  # noqa: S704 - values and attributes are escaped above.
            "<select "
            + _attrs({"name": name, **self.attrs, **(attrs or {})})
            + ">"
            + "".join(options)
            + "</select>"
        )
