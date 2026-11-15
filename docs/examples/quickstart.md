# Quickstart

Create a project scaffold, register a SQLAlchemy model, and open its generated
admin and JSON API.

## Requirements

- Python 3.11 or later
- [uv](https://docs.astral.sh/uv/)
- A SQLAlchemy 2 model and a database URL

## 1. Create a project and scaffold

```shell
uv init obox-demo --bare
cd obox-demo
uv add obox-builder
uv run obox startproject app
cd app
```

The scaffold includes `main.py`, `models.py`, `setting.py`, Alembic migration
files, and a small `justfile`. It uses SQLite and a development secret by
default. The parent `uv` project provides the environment for this generated
directory.

## 2. Define a model

Add this mapped class below `Base` in the generated `models.py`:

```python
class Question(Base):
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    question_text: Mapped[str] = mapped_column()
```

The generated `models.py` already imports `Mapped`, `mapped_column`, and defines
`Base` with SQLAlchemy optimistic versioning.

## 3. Register the resource

Create the `app` package and add these files:

```text
app/
├── __init__.py
├── apps.py
└── builders.py
```

In `app/builders.py`, define the fields and view for this resource:

```python
from obox_builder import ModelBuilder, builder
from models import Question


class QuestionBuilder(ModelBuilder[Question]):
    fields = ("question_text",)
    search_fields = ("question_text",)

    class View(builder.View):
        list_display = ("question_text",)
```

In `app/apps.py`, declare the builder:

```python
from obox_builder import AppConfig
from app.builders import QuestionBuilder


class PollsConfig(AppConfig):
    name = "app"
    label = "polls"
    builders = (QuestionBuilder,)
```

Set `INSTALLED_APPS` in `setting.py`:

```python
INSTALLED_APPS = ("app.apps.PollsConfig",)
```

Apps are loaded at startup. Their registered collections use the app label in
the API path, so this resource is available at `/api/polls/questions`.

## 4. Create the database and administrator

```shell
uv run alembic revision --autogenerate -m "add questions"
uv run alembic upgrade head
uv run obox createsuperuser
```

The generated migration environment includes both the project model metadata
and Auth's model metadata.

## 5. Run the application

```shell
uv run uvicorn main:app --reload
```

Open <http://127.0.0.1:8000/admin> and sign in. The generated superuser can
manage the registered resource.

Before deploying, set `OBOX_DEBUG=false` and provide a private
`OBOX_SECRET_KEY` of at least 32 bytes. For role-based access and custom row
scopes, continue with [Access control](../features/access-control.md).

## Next steps

- [Register a resource](features/register-resource.md) with more form and API options.
- [Query the JSON API](apis/query-resource.md) from a client.
- Browse [all implemented features](../features/index.md).
