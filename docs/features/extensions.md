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
when host code invokes its receipt service. It is retained as an opt-in helper;
it is not automatically applied to `ModelApi` routes. Include the feature's
model tables in host migrations and call `prune()` from a maintenance job to
remove expired receipts.

Analytics, files and activity integrate with shared model permissions and
runtime context. Activity capture and comment endpoints are described in the
[record activity contract](../record-activity-spec.md).
