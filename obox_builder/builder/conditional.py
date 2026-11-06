"""HTTP write conditions shared by record and comment adapters."""

import re
from collections.abc import Sequence

from obox_builder.builder.query import invalid
from obox_builder.errors import PreconditionFailed, PreconditionRequired


def check_match(values: Sequence[str], current: str, *, single: bool = False) -> None:
    if not values:
        raise PreconditionRequired("Send the object's current ETag.")
    value = ",".join(values).strip()
    tag = r'(?:W/)?"[\x21\x23-\x7e\x80-\xff]*"'
    element = r"\s*(?:" + tag + r")?\s*"
    if value == "*" and not single:
        return
    if not re.fullmatch(tag if single else element + r"(?:," + element + r")*", value):
        raise invalid("If-Match", "Use a valid entity tag.", "invalid_precondition")
    if current not in re.findall(tag, value):
        raise PreconditionFailed("The object has changed. Reload before saving.")
