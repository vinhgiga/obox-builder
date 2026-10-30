"""Field visibility for audit records uses the current resource contract."""

from dataclasses import dataclass

from obox_builder.contrib.history.tracker import get_tracker
from obox_builder.resources import OperationContext, get_registry


@dataclass(frozen=True)
class DiffMask:
    visible: frozenset[str]
    known: frozenset[str]
    is_superuser: bool = False

    def allows(self, name: str) -> bool:
        return name in self.visible


def diff_mask(model, ctx: OperationContext) -> DiffMask:
    tracker = get_tracker(model)
    known = (
        set() if tracker is None else {item.name for item in (*tracker.fields, *tracker.relations)}
    )
    registry = ctx.registry or get_registry()
    try:
        resource = registry.for_model(model)
    except LookupError:
        return DiffMask(frozenset(), frozenset(known))
    context = ctx.for_resource(resource)
    visible = {
        name
        for name, spec in resource.specs.items()
        if not spec.secret and resource.field_allowed(context, spec)
    }
    return DiffMask(
        frozenset(visible), frozenset(known | resource.specs.keys()), ctx.actor.is_superuser
    )
