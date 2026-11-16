# Release notes — 0.1.0

Obox v0.1 establishes a small Django 5.2 LTS / DRF 3.16.1-shaped core on FastAPI,
SQLAlchemy, Pydantic and Jinja2. Less code is better: core concepts have an explicit
reference mapping; product features stay optional contrib.

## Breaking changes

The distribution stays `obox-builder`, version0.1.0, with Python namespace `obox`.
Settings are lazy uppercase settings loaded from OBOX_SETTINGS_MODULE or configured
once. DATABASES, TEMPLATES, ROOT_URLCONF, INSTALLED_APPS and REST_FRAMEWORK replace
custom constructor configuration. Application startup is configs → models → ready.

Form and ModelForm are real independent classes. Generic View/TemplateView/list/
detail/editing classes replace the combined ModelView. REST is declared with
Serializer/ModelSerializer, ModelViewSet and SimpleRouter, replacing ModelApi.
ModelBuilder uses explicit model registration and owns only admin HTML.

All compatibility aliases and old plugin installation hooks are removed.
Database-aware hooks use async/await. get_queryset returns a Select without
executing it. Core HTTP behavior follows Django/DRF: invalid form200, editing
redirect302, serializer errors400 and successful REST DELETE204.

## Migration

1. Replace application imports with `obox` modules shown in the
   [Python reference](reference/python-api.md).
2. Rename the project settings module to settings.py, configure standard
   DATABASES/TEMPLATES and set OBOX_SETTINGS_MODULE before constructing Obox(app).
3. Define AppConfig.name and register the app in INSTALLED_APPS. Import model
   declarations before ready; register admin classes through app/admin.py.
4. Move form fields/validation into Form or ModelForm.Meta and clean hooks.
   Await validation and save; move fieldsets to ModelBuilder.
5. Replace combined views with generic views and explicit path/include patterns.
6. Define serializers/viewsets/router independently. Update clients for native ID
   types, declared field names, trailing slashes, DRF errors and PUT/PATCH semantics.
7. Register `(Model, BuilderClass)` or use @register(Model); remove generic model
   inference and form/view/API surface declarations from the builder.
8. Enable product features through contrib AppConfigs. Rich resource declarations
   use Resource plus explicit model and form_options/view_options/api_options.
   Product batch/metadata/concurrency clients retain their contrib protocol.

Application data is preserved by this refactor; review host migrations for the
contrib tables you enable. No database reset is part of the migration. Git history
and existing release tags are unchanged.

The [mapping table](mapping.md) lists the supported subset and tests. This release
does not promise complete Django/DRF compatibility. Runtime does not depend on
Django/DRF; their pinned reference environment is used only for contract tests.
