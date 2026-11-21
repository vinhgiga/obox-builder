# Django / DRF mapping — v0.1.0

Reference versions: [Django 5.2 LTS](https://docs.djangoproject.com/en/5.2/) and
[DRF 3.16.1](https://github.com/encode/django-rest-framework/tree/3.16.1).
Only the behavior below is part of the core contract. Native differences are
explicit; Obox does not implement the rest of either framework.

| Obox API | Reference API | Supported behavior | Native difference | Tests |
| --- | --- | --- | --- | --- |
| `conf.settings` | Django LazySettings/configure | lazy uppercase module settings; configure once; configuration errors | `OBOX_SETTINGS_MODULE` | `test_settings.py` |
| `apps.AppConfig`, registry | Django apps | constructor/create; metadata; configs → models → ready; duplicate/reentrant checks | SQLAlchemy model discovery | `test_settings.py` |
| `forms.Form` | Django Form | bound/unbound; fields; clean hooks; errors/add_error; initial/prefix | await validation before errors | `test_forms.py`, `test_forms_integration.py`, `test_reference_contracts.py` |
| `forms.ModelForm` | Django ModelForm | explicit Meta; scalar fields; unique/FK validation; save/commit=False | async persistence; validation avoids ORM mutation | `test_forms_integration.py` |
| `forms.widgets` | Django widgets | escaped inputs, checkbox, textarea, select, password/hidden | Jinja2/Markup rendering | `test_forms_integration.py` |
| `views.generic` | Django generic display/edit views | per-request instance; setup/dispatch; query/object/context/form hooks; invalid200/redirect303 | native request; Select; awaited DB work | `test_views.py` |
| `middleware.csrf` | Django CSRF | masked token, cookie/header/form input, origin checks | checked at view dispatch | `test_views.py` |
| `template.backends.jinja2` | Django Jinja2 backend | directories/app discovery/options; get_template/render | native Jinja2 environment | `test_views.py` |
| `Resource` | Django model validation and queryset conventions | shared query, input and write policy | async SQLAlchemy and Pydantic | `test_resources.py` |
| `api.ModelApi` | DRF model viewsets and routers | model CRUD; search, order and paging | native FastAPI and generated OpenAPI | `test_api.py` |
| `builder.ModelBuilder` | Django ModelAdmin options | form, fieldsets, list, search and actions | options for generated HTML and API routes | `test_builder.py` |
| `Obox(app)` | adapter exception | settings/apps/template/database/URL integration | FastAPI host adapter | `test_cli.py`, demo integration |

Reference contract tests run the same scalar input cases in a separate
Django/DRF environment, then compare output/errors with Obox. Product integration
and database tests additionally verify rollback and absence of writes during
validation. The reference suite is intentionally isolated from runtime dependencies.

Contrib resource editing, files, analytics, jobs, activity/comments, metadata,
batch/upsert and custom concurrency rules are product features. They are outside
this table and are documented under [contrib](features/index.md).
