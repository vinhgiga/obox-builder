# Python API

| Module | Public entry points |
| --- | --- |
| `obox` | `Obox`, `setup`, `__version__` |
| `obox.conf` | `settings` |
| `obox.apps` | `AppConfig`, `apps` |
| `obox.forms` | `Form`, `ModelForm`, scalar fields, widgets, `modelform_factory` |
| `obox.views.generic` | `View`, `TemplateView`, `ListView`, `DetailView`, `FormView`, `CreateView`, `UpdateView`, `DeleteView` |
| `obox.urls` | `path`, `include` |
| `obox.db.transaction` | `atomic` |
| `obox.builder` | `ModelBuilder`, `AdminSite`, `site`, `register`, `action` |
| `obox.rest_framework.serializers` | `Serializer`, `ModelSerializer`, scalar fields |
| `obox.rest_framework.views/generics/viewsets` | `APIView`, `GenericAPIView`, `GenericViewSet`, `ModelViewSet` |
| `obox.rest_framework.routers` | `SimpleRouter` |
| `obox.rest_framework.decorators` | `action` |
| `obox.rest_framework.permissions` | DRF-shaped permission classes |
| `obox.rest_framework.filters/pagination` | `SearchFilter`, `OrderingFilter`, `PageNumberPagination` |

Validation/persistence are awaited. Configuration and metadata stay synchronous.
There are no combined ModelView/ModelApi classes or generic builder model inference.

Product declarations live in `obox.contrib.resources`: Resource, FormOptions,
ViewOptions, ApiOptions, Inline, Policy and RBAC. Page/Component/Use are available
from `obox.contrib.resources.presentation`. These are product contracts, separate
from Form/ModelForm/generic views/REST. See [mapping](../mapping.md) and
[core contract](../core-spec.md) for behavior and limitations.
