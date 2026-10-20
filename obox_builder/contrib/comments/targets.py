"""The polymorphic target identity shared by comments and row history."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommentTarget:
    """One row that comments attach to (``model_label`` + stringified PK)."""

    model_label: str
    object_pk: str
    object_repr: str = ""

    def with_repr(self, repr_: str) -> CommentTarget:
        return CommentTarget(self.model_label, self.object_pk, repr_[:255])


__all__ = ["CommentTarget"]
