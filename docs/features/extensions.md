# Background work and contrib

Contrib apps are enabled through AppConfig and INSTALLED_APPS. There is one
application lifecycle: configs, model imports, then ready. There is no separate
public plugin install/setup lifecycle.

`obox.contrib.jobs` provides durable tasks, attempts, retries, revocation and
workers. Register functions with its task decorator, import task modules in the
worker, and run `obox jobs worker`. The CLI also supports enqueue, status,
retry, revoke and retention cleanup. Workers run as separate host processes.

`obox.contrib.webhooks` stores subscriptions/deliveries and sends signed requests
with bounded retries. Configure WEBHOOKS with signing keys and trusted identity/
destination controls. `obox.contrib.idempotency` stores caller-scoped receipts
for supported resource creates/batch writes. Both require their model tables in
host migrations and are product extensions rather than REST CRUD requirements.

Analytics, files and activity reuse contrib resource services and policies.
Product redesign is deferred; v0.1 updates only their integration with the new
namespace/settings/AppConfig lifecycle.
