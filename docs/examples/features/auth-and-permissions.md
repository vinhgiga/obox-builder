# Authentication and permissions

Include `obox.contrib.auth` in `INSTALLED_APPS` and run the auth migrations from
the scaffold. Create the first administrator with `uv run obox createsuperuser`.

Open `/auth/login`, then use `/auth/users` and `/auth/roles` to grant model and
custom route permissions. The account page is governed by `auth.use_account`.
Installing the API token feature with `AUTH = {"api_tokens": True}` allows
users with account access to create scoped tokens.

See [access control](../../features/access-control.md) for a protected native
FastAPI route and the default model permission names.
