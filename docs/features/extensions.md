# Background work and extensions

Use optional extension points to keep the host application in control of
business workflows and infrastructure.

## Apps, pages, and components

`AppConfig` groups builders, components, pages, and FastAPI routers under an
application label. Pages compose components such as analytics views. Host
routers remain ordinary FastAPI routers; use Obox integration when a router
needs the admin UI's locale and route-conflict handling.

## Database-backed jobs

`obox_builder.contrib.jobs` provides registered task functions, durable task
records, retries, revocation, and a standalone worker. Run a worker in a
separate process with `obox jobs worker`; the CLI also supports enqueue, status,
retry, revoke, and retention cleanup. Import task modules explicitly or use the
default task module detected from settings.

## Webhooks

Install `Webhooks` to publish durable change notifications and deliver signed
requests from a worker process. Configuration requires signing keys, a current
identity resolver, and policy controls for destinations. Delivery has bounded
retries and hosts can prune expired subscriptions and events.

## Idempotency

Install `Idempotency` to add opt-in request receipts for supported resource
creates and batch writes. A repeated key with the same request replays the
stored result; reusing a key for a different payload is a conflict. Receipts
are scoped to the authenticated caller and require the plugin's table in host
migrations. SQLite and PostgreSQL are supported.

These extensions use the host database, migrations, settings, and worker
lifecycles. Read each extension's API module and tests before choosing a
deployment topology; they do not create managed worker processes automatically.
