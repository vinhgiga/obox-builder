# Event System — Architecture, System Design & Database Model

**Project:** obox-builder · **Branch:** `dev` @ `b35df06` (v0.2.1) · **Ngày:** 2026-10-21 · **Trạng thái:** Proposed (đề xuất, chưa triển khai) · **Vị trí đề xuất:** `docs/adr/0003-events.md`

> **Phạm vi.** Tính năng *event* cho FastAPI admin tự xây: (1) **nhận** sự kiện (webhook inbound, lịch, thao tác thủ công, domain event từ code); (2) **phát** sự kiện (thay đổi dữ liệu, domain event) tới handler, worker, webhook ngoài, realtime UI; (3) **đánh thức worker** với độ trễ thấp nhưng vẫn đúng khi thông báo bị mất. Cơ sở: (a) đọc code nhánh `dev`; (b) research tài liệu kỹ thuật về hệ thống event (Directus Flows, Hasura Event Triggers, PostgreSQL NOTIFY, Standard Webhooks, outbox/queue trên Postgres); (c) một số giả định kỹ thuật đã **kiểm chứng bằng thực nghiệm** (đánh dấu ✔ verified).

> **Ghi chú số ADR.** `docs/adr/` trên `dev` chỉ có `0001-row-history.md`, nhưng `comments/models.py` đã trích dẫn «ADR-0002 D1» (file này không nằm trong cây). Tài liệu dùng số **0003**; đổi lại nếu 0002 được đặt khác.

---

## 0. Tóm tắt quyết định

| # | Quyết định | Lý do ngắn |
| --- | --- | --- |
| 1 | **Transactional outbox**: ghi `events_event` + `events_delivery` trong *cùng transaction* với dữ liệu (SQLAlchemy `after_flush` + `before_commit`) | Không dual-write; rollback ⇒ không có event; cùng cơ chế đã chạy ổn của `contrib.history` |
| 2 | **DB là nguồn sự thật; mọi cơ chế wake-up chỉ là gợi ý** (in-process `asyncio.Event`, `LISTEN/NOTIFY`, poll) | Thông báo có thể bị gộp, trùng lặp hoặc bị mất; NOTIFY mất khi listener rớt |
| 3 | Hai bảng lõi: **`events_event`** (append-only) và **`events_delivery`** (state machine, claim bằng `FOR UPDATE SKIP LOCKED`); **fan-out lúc ghi** | Không phụ thuộc cursor theo `id` ⇒ không bỏ sót event commit trễ |
| 4 | **NOTIFY chỉ gửi từ notifier gộp batch, sau commit**; không bao giờ trong transaction nghiệp vụ; poll fallback luôn bật | NOTIFY lấy global lock lúc commit; hàng đợi NOTIFY đầy làm *commit fail* |
| 5 | Giao hàng **at-least-once** + `idempotency_key`; handler `transactional=True` cho exactly-once *effect* trên DB | Đơn giản, an toàn, kiểm chứng được |
| 6 | **Port `core.events`** (emit/on) ở tầng thấp, **adapter `contrib.events`** nằm trên `history\|comments` | auth/comments/history phát event mà không import ngược lên |
| 7 | Data-change capture **tái dùng** `Tracker`, `FieldType.diff_repr`, masking của history; opt-in bằng `__events__` | Một engine diff; secret không bao giờ rời hệ thống |
| 8 | **Inbound** `/events/in/{slug}`: bắt buộc HMAC (Standard Webhooks + generic), dedupe, ack `202` rồi xử lý bất đồng bộ | Xác minh nguồn gửi và tính toàn vẹn của payload trước khi nhận sự kiện |
| 9 | **Outbound webhook** ký Standard Webhooks; retry backoff + full jitter ≈ 1 ngày; circuit breaker; SSRF guard | Thực hành của Stripe, Hook0, Hookdeck |
| 10 | **Tự xây**, portable SQLite + PG; không dùng Procrastinate/Celery | NF4 (SQLite dev/test); vẫn phải tự xây subscription/inbound/replay |
| 11 | 5 phase; **Phase 1** = domain event + data-change + handler + worker (in-process wake + poll) | Giá trị sớm, rủi ro thấp |

---

## 1. Bối cảnh: `dev` đã có / còn thiếu

**Đã có (tái sử dụng):**

- `core/signals.Signal`: đồng bộ, in-process, không bền vững, **không có pha «sau commit»** ⇒ không dùng làm event bus tin cậy (chỉ hợp làm hook cục bộ).
- `history.post_record` và `comments.post_comment` đều bắn **trong** transaction của người ghi. Docstring `comments/signals.py` đã dặn «dùng outbox ở Phase 3 cho hiệu ứng bền vững bên ngoài» ⇒ nhu cầu outbox dùng chung đã được dự báo. Lưu ý: `conf/base.py` ghi `COMMENTS_OUTBOX_MAX_ATTEMPTS` là Phase 3, còn `docs/comments.md` ghi Phase 2 — nên thống nhất khi duyệt ADR này.
- `contrib/history/capture.py`: mẫu capture hoàn chỉnh (`before_flush` → `after_flush` → `after_commit/after_rollback`, khoá `session.info`, `HISTORY_FAILURE_MODE`, bulk guard `do_orm_execute`, signals `pre/post_record`).
- `history/context.py`: `HistoryContext`, `bind()` (late-bound theo request), `use()` (ContextVar cho CLI/job); `SOURCES` đã có `job`.
- `history/tracker.py` + `payload.py`: `Tracker`, `PendingChange`, mask/redact/truncate, `FieldType.diff_repr`.
- `db/session.py`: `get_db` = 1 transaction/request; `session_scope()` cho CLI/job; `expire_on_commit=False`.
- `db/engine.py`: async-only; driver `psycopg_async`, `asyncpg`, `aiosqlite`; `to_sync_url`.
- `core.signals.post_migrate` + `AppConfig.ready()` + `AppConfig.permissions`: cơ chế đồng bộ cấu hình sau migrate (như `sync_permissions`).
- `management/commands` với `@command` hỗ trợ coroutine; `run_async()` dispose engine khi xong.
- Quy ước model: `VersionedMixin`, `TimestampMixin`, `BigIntIdentity`, `JSONB` (variant), naming convention, khai báo ở class (`__history__`, `__comments__`).
- import-linter (layers), `BaseSettings(extra='forbid')`, dải mã check: admin 1xx, history 2xx, comments 3xx ⇒ events dùng **4xx**.
- CI chạy 2 lần (SQLite và Postgres 17); `docker-compose` chỉ có service `db`.

**Còn thiếu / rủi ro cần xử lý:**

- Không có **worker/tiến trình nền**: `core/application.lifespan` chỉ tạo engine rồi `dispose()`.
- Chưa có HTTP client ở runtime (`httpx` mới là dev dependency), cron parser, hay mã hoá secret at-rest (`pwdlib[argon2]` chỉ để hash).
- `utils/module_loading.py` chỉ có `import_string`, chưa có `autodiscover_modules`.
- `after_commit` của SQLAlchemy là callback **đồng bộ**: ở đó chỉ được làm việc không I/O (đẩy vào buffer).
- ✔ **Verified** (SQLAlchemy 2.0.55, SQLite 3.45.1): commit một session «sạch» **không** gọi `before_flush/after_flush`, chỉ gọi `before_commit/after_commit`; và `before_commit` **có thể** chạy INSERT được commit cùng transaction. ⇒ event do `publish()` (không kèm thay đổi ORM) phải được ghi ở `before_commit`, không chỉ ở flush.
- SQLite không có `SKIP LOCKED`/`LISTEN` ⇒ cần chiến lược theo dialect. ✔ **Verified:** `with_for_update(skip_locked=True)` biên dịch thành `FOR UPDATE SKIP LOCKED` trên PG và **bị lược bỏ** trên SQLite; `UPDATE … RETURNING` chạy được trên SQLite ≥ 3.35.
- Ghi bằng bulk/raw SQL/service khác không đi qua unit of work (như ADR 0001 §6.3) ⇒ nằm ngoài phạm vi capture tự động.
- Route inbound phải **không** dùng cookie/CSRF của admin; cần xác minh phạm vi của `contrib/auth/csrf.py`.

---

## 2. Research

### 2.1 Cơ sở kỹ thuật

| Hệ thống | Điều rút ra |
| --- | --- |
| **Directus Flows** | 5 loại trigger: event hook, webhook, schedule (CRON), another flow, manual. Event hook có 2 kiểu: **Filter (blocking)** chạy *trong* transaction và có thể veto/biến đổi payload; **Action (non-blocking)** để event commit rồi mới chạy flow. |
| **Hasura Event Triggers** | Giao at-least-once và có thể lệch thứ tự; `retry_conf {num_retries, interval_sec, timeout_sec}` (mặc định 0 / 10 / 60); `enable_manual`; API `redeliver_event` và `invoke_event_trigger` (payload tuỳ ý); invocation logs; scheduled event có `tolerance_seconds` (quá trễ thì bỏ); trigger update chỉ nghe các cột chỉ định; secret tham chiếu qua `*_from_env`. |
| **PostgreSQL NOTIFY** (docs PG 18) | Chỉ giao *sau commit*; cùng channel + payload trong một transaction bị gộp; payload < 8000 byte; hàng đợi đầy ⇒ transaction gọi NOTIFY **fail lúc commit**; không dùng được với 2PC. |
| **Recall.ai, DBOS** | Commit của transaction có NOTIFY lấy **global lock** ⇒ serialize mọi commit khi nhiều writer; bản vá PG 19 không gỡ global lock. Cách khắc phục của DBOS: coi NOTIFY là *hint*, **buffer và flush theo batch**, thêm poll fallback tần suất thấp (đạt \~60k ghi/giây). |
| **Outbox ordering** | Sequence cấp *trước* commit ⇒ consumer dùng cursor theo `id` có thể bỏ sót dòng commit muộn. Giải pháp: lọc theo `pg_snapshot_xmin`, hoặc dùng delivery row + `SKIP LOCKED` thay cursor. |
| **Standard Webhooks** | Header `webhook-id`, `webhook-timestamp`, `webhook-signature`; ký trên `id.timestamp.rawBody`; `v1,<base64>` (HMAC-SHA256) hoặc `v1a` (Ed25519); nhiều chữ ký cách nhau bằng dấu cách để **xoay khoá**; tolerance mặc định 5 phút; dedupe theo `webhook-id`; phải ký/verify trên **raw bytes**. |
| **Thực hành outbound** (Hookdeck, Hook0, Stripe) | Retry khi 5xx/408/429/lỗi mạng; 4xx khác ⇒ dead ngay; `Retry-After` ghi đè backoff; cửa sổ retry 1–3 ngày (Stripe 3 ngày, Hook0 mặc định 25 lần/8 ngày); full jitter; tự vô hiệu endpoint sau thời gian lỗi dài **kèm thông báo**; chặn SSRF (private/loopback/link-local/metadata); đối tác thường cho endpoint 5–10 giây. |
| **Procrastinate** (Python) | Task queue trên Postgres: PG 13+, psycopg3, async-first, `SKIP LOCKED` + LISTEN, retry/periodic/lock. **Chỉ Postgres** ⇒ loại (xem §14). |

### 2.2 Yêu cầu → quyết định thiết kế

| # | Yêu cầu | Quyết định |
| --- | --- | --- |
| L1 | Ping mỏng + pull có cursor | Wake-up nội bộ cũng «mỏng»: NOTIFY chỉ mang tên queue; `format='thin'` + pull API ở Phase 4 |
| L2 | Thông báo at-least-once có thể trùng hoặc bị gộp | Wake-up chỉ là hint; worker luôn đọc bảng; handler idempotent |
| L3 | Giữ payload để catch-up khi delivery bị tạm dừng | Subscription tắt/circuit mở **không** ngừng ghi delivery; giữ pending để catch-up; có `events_replay` |
| L4 | Dedupe theo ID ổn định | `event_id` UUID, `dedup_key`, `UNIQUE(event_id, subscription_id)`, `UNIQUE(source_id, external_id)` |
| L5 | Lọc ở nguồn theo loại thay đổi, cột và điều kiện | Subscription có `event_types`, `watch_fields`, `when`, `origins`, đánh giá lúc fan-out |
| L6 | Giá trị trước thay đổi là opt-in | `previous=False` mặc định (nhỏ hơn, ít rò rỉ PII) |
| L7 | Payload theo ID để chịu đổi tên | `schema_version` + type ổn định; đổi tên field = bump version |
| L8 | Inbound cần xác thực chữ ký | HMAC bắt buộc; `allow_unsigned` phải khai báo rõ và bị cảnh báo; secret hiển thị 1 lần |
| L9 | Giới hạn tốc độ và thời gian xử lý request | Ack `202` sau khi ghi bền vững; xử lý async; rate limit + `Retry-After` |
| L10 | Retry có giới hạn và tự tắt có thông báo | Backoff + jitter, dead-letter, circuit breaker, thông báo |
| L11 | Ngăn vòng lặp trigger | `hop`, `causation_id`, chặn self-trigger |
| L12 | Ghi ngoài app không đi qua capture | Bulk guard + (tuỳ chọn) DB trigger; tài liệu hoá |
| L13 | Tránh thundering herd khi nhiều lịch đến hạn cùng lúc | `jitter_seconds` cho schedule; `dedup_key` theo tick; `SKIP LOCKED` |
| L14 | Ghi tuần tự cùng row khi được yêu cầu | `ordered` + `ordering_key` (opt-in) |
| L15 | Run history, retention, cảnh báo lỗi, revision history | `events_delivery_attempt`, DLQ UI, `__history__` cho bảng cấu hình |
| L16 | Quota tường minh | `concurrency_limit`, `rate_limit_per_minute`, `EVENTS_MAX_PAYLOAD_BYTES`, retention |
| L17 | Manual trigger / redeliver | `events_emit`, redeliver, nút *test ping* |
| L18 | Tách guard đồng bộ và action bất đồng bộ | v1 chỉ làm **action bất đồng bộ**; guard đồng bộ vẫn là signals hiện có |

---

## 3. Yêu cầu

**Chức năng**

- **F1** Phát domain event từ code trong cùng transaction với dữ liệu.
- **F2** Tự phát event thay đổi dữ liệu (create/update/delete + m2m) cho model khai báo `__events__`; payload tôn trọng masking/redaction.
- **F3** Subscription: handler trong code; webhook ngoài cấu hình ở admin; (Phase 4) queue pull cho worker ngoài. Lọc theo type/pattern, field theo dõi, điều kiện khai báo, nguồn.
- **F4** Nhận event: inbound webhook có xác thực + dedupe; schedule (cron/interval/once); thủ công (admin/CLI).
- **F5** Worker được đánh thức tức thì, scale ngang, graceful shutdown; retry/backoff/dead-letter; ordering tuỳ chọn.
- **F6** Admin UI: feed, deliveries, DLQ (retry/discard/replay), subscriptions, endpoints, inbound sources, schedules, workers.
- **F7** Realtime stream (SSE) có resume (`Last-Event-ID`) và lọc theo quyền.
- **F8** Replay/backfill cho subscription mới; redeliver thủ công.

**Phi chức năng**

- **NF1 Atomic:** event commit ⇔ dữ liệu commit.
- **NF2 Overhead thấp:** mỗi flush thêm tối đa 1 `INSERT` cho `events_event` và 1 cho `events_delivery` (executemany); đo bằng benchmark như `tests/history/test_write_cost.py`.
- **NF3 Không mất event:** at-least-once qua crash worker, rớt listener, mất NOTIFY. Độ trễ wake-up mục tiêu p50 < 100 ms khi có in-process wake/NOTIFY; ≤ chu kỳ poll khi fallback.
- **NF4 Portable:** SQLite (dev/test) + Postgres (prod); tính năng PG-only phải optional.
- **NF5 Layering** đúng import-linter.
- **NF6 Bảo mật:** inbound được xác thực; outbound được ký + chặn SSRF; secret không lộ ở UI/log/history; field nhạy cảm không rời hệ thống.
- **NF7 Observable:** metrics, log, signals, system checks, CLI.
- **NF8 Schema evolution:** `schema_version`; thêm key là tương thích ngược.
- **NF9 Vận hành:** retention theo từng bảng, purge theo batch, sẵn sàng partition.

**Ngoài phạm vi v1:** workflow nhiều bước/DAG; guard đồng bộ có thể veto transaction (Directus *filter*); exactly-once xuyên hệ thống; CDC logical decoding/Debezium; message broker ngoài (Kafka/RabbitMQ); biến đổi payload bằng script do người dùng nhập (rủi ro thực thi mã tuỳ ý).

---

## 4. Kiến trúc

### 4.1 Component view

&#91;embedded content: component view · producers, outbox transaction, worker, đường đánh thức\]

Producer nào cũng đi qua cùng một cửa: ghi `events_event` + `events_delivery` **trong transaction của chính nó**. Worker chỉ đọc bảng; NOTIFY, `asyncio.Event` và poll chỉ rút ngắn thời gian chờ, không bao giờ là kênh mang dữ liệu.

### 4.2 Layering (import-linter)

```text
layers = [
  'obox_builder.contrib.admin',
  'obox_builder.contrib.events',   # MỚI: import được history/comments/auth; không bị chúng import
  'obox_builder.contrib.history | obox_builder.contrib.comments',
  'obox_builder.contrib.auth',
  'obox_builder.core', ...         # core.events = PORT mới nằm ở đây
]
```

Vấn đề: `auth`, `comments`, `history` (và code ứng dụng) cần *phát* event nhưng không được import `contrib.events` (tầng trên). Giải pháp **Ports & Adapters**, cùng tinh thần `Model.__history__` (khai báo, không import):

```python
# obox_builder/core/events.py  (PORT: không I/O, không import db)
def emit(session, type, payload=None, *, subject=None, dedup_key=None, **opts) -> None:
    session.info.setdefault("obox.events.drafts", []).append(
        EventDraft(type, payload, subject, dedup_key, opts)
    )


def on(pattern, *, queue="default", **opts):  # decorator: chỉ đăng ký handler vào registry
    ...
```

- `contrib.events` (**adapter**) đọc drafts + registry, ghi bảng, chạy worker. Nếu app `events` không được cài, `emit()` là **no-op** (Null Object) nên `auth`/`comments` vẫn chạy bình thường.
- Tái dùng `history.context` (`bind`, `use`, `resolve_context`) cho actor/source/request\_id: `events` nằm trên `history` nên được phép import. Tuỳ chọn Phase 0: tách thành `core.context.ActorContext` để events không phụ thuộc việc history có được cài hay không.
- Handler trong code đăng ký qua `core.events.on(...)`; `contrib.events` autodiscover module `<app>.events` của mọi app trong `INSTALLED_APPS` (cần thêm `autodiscover_modules` vào `utils/module_loading.py`).

### 4.3 Write path (producer)

1. Producer: (a) model có `__events__` bị thay đổi; (b) `await events.publish(db, 'shop.order.shipped', {...}, subject=order)` hoặc `core.events.emit(session, ...)`; (c) route inbound; (d) scheduler tick.
2. `before_flush`: tính `PendingEvent` từ *attribute history* (không I/O, an toàn với `lazy='raise'`) bằng `Tracker`; gộp với drafts trong `session.info`.
3. SQLAlchemy phát INSERT/UPDATE/DELETE; PK của dòng mới đã có.
4. `after_flush`: (i) `fill_create`/`fill_m2m` như history; (ii) **1 executemany** `INSERT events_event` (lấy `id` bằng `RETURNING`); (iii) khớp subscription trong **cache in-memory** rồi **1 executemany** `INSERT events_delivery ... ON CONFLICT DO NOTHING`; (iv) ghi nhớ các `queue` bị chạm vào `session.info`.
5. `before_commit`: xử lý nốt các draft còn lại. ✔ Bắt buộc vì session sạch không gọi flush hook (đã verify ở §1).
6. Commit cuối request (`get_db`) ⇒ **atomic**. Rollback hay savepoint rollback ⇒ event và delivery biến mất cùng dữ liệu.
7. `after_commit` (đồng bộ, **không I/O**): đẩy tên queue vào `WakeBuffer` của process và `asyncio.Event.set()` cho worker nhúng cùng process. `after_rollback`: xoá pending/drafts.
8. **Notifier** (task nền, §6.2) flush buffer theo lô.

`RETURNING id` với executemany dựa trên *insertmanyvalues* của SQLAlchemy 2.0 — cần spike xác nhận trên `psycopg_async`, `asyncpg`, `aiosqlite`; phương án dự phòng: `events_delivery` tham chiếu `event_uuid` thay vì `event_id`.

### 4.4 Vòng đời delivery

&#91;embedded content: state machine của events\_delivery · 5 trạng thái\]

Mọi chuyển trạng thái do worker thực hiện đều có **fencing**: `UPDATE ... WHERE id = :id AND locked_by = :worker AND status = 'running'`. Nếu lease đã mất và worker khác đã nhận việc, câu lệnh ảnh hưởng 0 dòng và kết quả của worker cũ bị bỏ.

### 4.5 Design patterns áp dụng

| Pattern | Dùng ở đâu | Vì sao |
| --- | --- | --- |
| Transactional Outbox | `events_event` + `events_delivery` ghi cùng transaction | Không dual-write |
| Inbox / Idempotent Receiver | `events_inbound_receipt`, `UNIQUE(event_id, subscription_id)`, `idempotency_key` | Chịu được trùng lặp của at-least-once |
| Competing Consumers | N worker claim bằng `SKIP LOCKED` | Scale ngang, không cần leader |
| Ports & Adapters | `core.events` (port) và `contrib.events` (adapter) | Giữ đúng layering |
| Null Object | `emit()` khi app events không cài | auth/comments không phụ thuộc |
| Observer + Unit of Work | Session events; `flush` là ranh giới batch | 1 insert mỗi flush |
| State Machine + Fencing token | `events_delivery.status` + `locked_by` | Chặn ghi đè từ worker đã mất lease |
| Lease / Visibility timeout | `locked_until` + reaper | Phục hồi sau crash |
| Retry + Exponential Backoff + Full Jitter | tính `run_at` | Tránh retry đồng bộ |
| Circuit Breaker | `events_endpoint` | Không đốt attempt khi endpoint đang chết |
| Dead Letter Queue | `status='dead'` + admin UI | Không mất sự kiện lỗi |
| Hint + Source of Truth | in-process wake, NOTIFY, poll | Đúng cả khi mất thông báo |
| Strategy | claim theo dialect; `Verifier` inbound; `Formatter` outbound | PG và SQLite; nhiều chuẩn chữ ký |
| Registry | handler registry, `SubscriptionIndex` | Giống `site.register` |
| Specification | `when` là AST khai báo | Không thực thi code người dùng nhập |
| Command | replay, redeliver, emit thủ công | Có audit |
| Claim Check | `format='thin'`: chỉ id, bên nhận tự pull | Payload lớn hoặc nhạy cảm |
