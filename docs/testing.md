# Testing

obox-builder ships a pytest plugin that registers itself through the
`pytest11` entry point, so once the package is installed there is no fixture
wiring to do — just point it at your settings module.

## Setup

Install `pytest` and `pytest-asyncio`, and enable asyncio auto mode in your
project's `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

The plugin needs to know which settings module to load. Export
`OBOX_SETTINGS_MODULE`, or set it early in `tests/conftest.py`:

```python
# tests/conftest.py
import os

os.environ.setdefault("OBOX_SETTINGS_MODULE", "config.settings")
```

## Fixtures

| Fixture    | What it does                                                             |
| ---------- | ------------------------------------------------------------------------ |
| `obox_env` | Loads the settings module and calls `obox_builder.setup()`.               |
| `db`       | Creates every registered table, syncs permissions and yields an `AsyncSession`. |
| `client`   | An `httpx.AsyncClient` wired to your project's ASGI application.          |
| `login`    | Creates a user, opens a session and sets the session cookie on `client`.  |

The `db` fixture rolls back at teardown and uses the database from
`DATABASE_URL`.

!!! tip
    Point tests at an in-memory database with
    `OBOX_DATABASE_URL=sqlite+aiosqlite:///:memory:` (or by overriding
    `DATABASE_URL` in a test settings module). Every pytest run then starts from
    an empty database.

## Factories

`obox_builder.testing` exports helpers for building state quickly:

- `make_user(username, password=DEFAULT_PASSWORD, *, email="", is_superuser=False, ...)`
- `make_role(name, *permissions, description="", is_system=False)`
- `grant_permissions(user, *permissions)`
- `DEFAULT_PASSWORD` — `"s3cure-password"`

Permissions are `app_label.codename` strings such as `"auth.view_user"` or
`"blog.publish_post"`.

## Example

```python
import httpx

from obox_builder.testing import grant_permissions, make_user


async def test_dashboard_requires_login(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/", headers={"accept": "text/html"})

    assert response.status_code == 303


async def test_superuser_opens_dashboard(client: httpx.AsyncClient, login) -> None:
    await login("alice", is_superuser=True)

    response = await client.get("/admin/", headers={"accept": "text/html"})

    assert response.status_code == 200
    assert "Dashboard" in response.text


async def test_user_list_requires_permission(client: httpx.AsyncClient, login) -> None:
    await login("bob")  # bob has no permissions

    response = await client.get("/admin/users", headers={"accept": "text/html"})

    assert response.status_code == 403


async def test_granting_a_permission_opens_the_user_list(client: httpx.AsyncClient, login) -> None:
    user = await make_user("carol")
    await grant_permissions(user, "auth.view_user")
    await login("carol")

    response = await client.get("/admin/users", headers={"accept": "text/html"})

    assert response.status_code == 200
```

!!! warning
    Reusing a username with `make_user` resets the password, which revokes the
    user's sessions. Create users and grant permissions **before** calling
    `login`.
