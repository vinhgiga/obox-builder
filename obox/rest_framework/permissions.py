from typing import Any, ClassVar

from obox._async import invoke
from obox.views.generic.detail import _model_label

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


class BasePermission:
    def has_permission(self, request: Any, view: Any) -> Any:
        return True

    def has_object_permission(self, request: Any, view: Any, obj: Any) -> Any:
        return True


class AllowAny(BasePermission):
    pass


class IsAuthenticated(BasePermission):
    def has_permission(self, request: Any, view: Any) -> Any:
        return bool(getattr(request.user, "is_authenticated", False))


class IsAdminUser(BasePermission):
    def has_permission(self, request: Any, view: Any) -> Any:
        return bool(getattr(request.user, "is_staff", False))


class IsAuthenticatedOrReadOnly(BasePermission):
    def has_permission(self, request: Any, view: Any) -> Any:
        return request.method in SAFE_METHODS or bool(
            getattr(request.user, "is_authenticated", False)
        )


class DjangoModelPermissions(BasePermission):
    perms_map: ClassVar[dict[str, list[str]]] = {
        "GET": [],
        "HEAD": [],
        "OPTIONS": [],
        "POST": ["%(app_label)s.add_%(model_name)s"],
        "PUT": ["%(app_label)s.change_%(model_name)s"],
        "PATCH": ["%(app_label)s.change_%(model_name)s"],
        "DELETE": ["%(app_label)s.delete_%(model_name)s"],
    }

    def get_required_permissions(self, method: str, model_cls: type[Any]) -> list[str]:
        from obox.rest_framework.exceptions import MethodNotAllowed

        if method not in self.perms_map:
            raise MethodNotAllowed(method)
        label, name = _model_label(model_cls)
        return [
            value % {"app_label": label, "model_name": name} for value in self.perms_map[method]
        ]

    async def has_permission(self, request: Any, view: Any) -> Any:
        if not getattr(request.user, "is_authenticated", False):
            return False
        query = await invoke(view.get_queryset)
        model = query.column_descriptions[0]["entity"]
        for permission in self.get_required_permissions(request.method, model):
            if not await invoke(request.user.has_perm, permission):
                return False
        return True
