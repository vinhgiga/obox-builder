"""HTML presentation binding for the shared import policy."""

from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.imports.resources import ImportResource
from obox_builder.resources import ModelResource


class AdminImportResource(ImportResource):
    def __init__(self, ctx: AdminContext) -> None:
        if ctx.admin is None or not isinstance(ctx.admin.resource, ModelResource):
            raise ValueError("A model view is required.")
        super().__init__(ctx.admin.resource, ctx.operation_context())
