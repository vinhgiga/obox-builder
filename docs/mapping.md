# Django / DRF mapping — v0.1.0

Reference versions: [Django 5.2 LTS](https://docs.djangoproject.com/en/5.2/) and
[DRF 3.16.1](https://github.com/encode/django-rest-framework/tree/3.16.1).
Only the behavior below is part of the core contract. Native differences are
explicit; Obox does not implement the rest of either framework.

| Obox API | Reference API | Supported behavior | Native difference | Tests |
| --- | --- | --- | --- | --- |
| `conf.settings` | Django LazySettings/configure | lazy uppercase module settings; configure once; configuration errors | `OBOX_SETTINGS_MODULE` | `test_settings.py` |
| `apps.AppConfig`, registry | Django apps | constructor/create; metadata; configs → models → ready; duplicate/reentrant checks | SQLAlchemy model discovery | `test_settings.py` |
| `forms.Form` | Django Form | bound/unbound; Pydantic schema validation; errors/add_error; initial/prefix | await validation before errors | `test_forms.py`, `test_forms_integration.py`, `test_reference_contracts.py` |
| `forms.ModelForm` | Django ModelForm | explicit Meta; scalar fields; unique/FK validation; save/commit=False | async persistence; validation avoids ORM mutation | `test_forms_integration.py` |
| `forms.widgets` | Django widgets | escaped inputs, checkbox, textarea, select, password/hidden | Jinja2/Markup rendering | `test_forms_integration.py` |
| `views.generic` | Django generic display/edit views | Per-request instance, setup/dispatch, query/object/context/form hooks, invalid forms return 422, successful submissions redirect with 303 | Native request, SQLAlchemy `Select`, awaited DB work | `test_views.py` |
| `middleware.csrf` | Django CSRF | masked token, cookie/header/form input, origin checks | checked at view dispatch | `test_views.py` |
| `template.backends.jinja2` | Django Jinja2 backend | directories/app discovery/options; get_template/render | native Jinja2 environment | `test_views.py` |
| `ModelResource` | Django model validation and queryset conventions | shared query, input and write policy | async SQLAlchemy and Pydantic | `test_resources.py` |
| `api.ModelApi` | DRF model viewsets and routers | model CRUD; search, order and paging | native FastAPI and generated OpenAPI | `test_api.py` |
| `views.generic.ModelViewSet` | Django ModelAdmin options | list display/filters/search/actions; fieldsets, inlines and form class | binds view classes to a `ModelResource` for generated routes | `test_registered_views.py`, `test_view_inlines.py` |
| `Obox(app)` | adapter exception | settings/apps/template/database/URL integration | FastAPI host adapter | `test_cli.py`, demo integration |

The isolated Django 5.2 reference suite runs six scalar `Form` cases and
compares cleaned data and errors with Obox. The other rows link to Obox's own
tests. Those tests verify the local contract, including rollback and absence
of writes during validation. The Django and DRF columns describe the APIs
that inspired the corresponding Obox behavior.

Contrib resource editing, files, analytics, jobs, activity/comments, metadata,
batch/upsert and custom concurrency rules are product features. They are outside
this table and are documented under [contrib](features/index.md).
