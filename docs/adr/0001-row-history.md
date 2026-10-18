# Row History — Kiến trúc, System Design & Database Model

**Dự án:** obox-builder · **Branch:** `dev` @ `dabd36d` (v0.2.1) · **Ngày:** 18/10/2026 · **Trạng thái:** Accepted — Phase 1 (MVP) đã triển khai

> **Phạm vi:** tính năng *row history* cho admin FastAPI tự triển khai: ghi lại *ai* đổi *gì*, *khi nào*, *từ đâu* trên từng row; hiển thị timeline; về sau hỗ trợ hoàn tác. Tài liệu dựa trên (1) đọc code branch `dev`, (2) nghiên cứu Airtable, AppSheet, Jotform và các hệ thống tham chiếu (Directus, Salesforce, django-auditlog, supa\_audit, SQLAlchemy).

> **Trạng thái triển khai (18/10/2026):** Phase 1 hoàn thành — xem
> [docs/history.md](../history.md) cho hướng dẫn sử dụng. Khác biệt nhỏ so với
> RFC: bổ sung setting `HISTORY_TRACK_ALL_ADMIN_MODELS`; timeline dùng keyset
> `?before=<id>` như thiết kế; card nằm trong `templates/admin/model/form.html`
> (model thường), `templates/admin/users/form.html` và
> `templates/admin/roles/form.html` (trang bespoke), trang timeline chung ở
> `templates/admin/history.html` (chưa có mục "Audit log" toàn cục — thuộc
> Phase 2); check `W201` (VersionedMixin) chưa kích hoạt vì `VersionedMixin` là
> Phase 3.

## 0. Tóm tắt quyết định

| # | Quyết định | Lý do ngắn |
| --- | --- | --- |
| 1 | Capture ở tầng **SQLAlchemy Session** (`before_flush` → `after_flush`), cùng transaction với dữ liệu | Biết actor/request, có `FieldType`, portable SQLite + Postgres, atomic |
| 2 | **Một bảng fact append-only** `history_row_change`: 1 dòng = 1 row bị đổi trong 1 flush; diff dạng JSONB `{field: [old, new]}` | Compact, đọc timeline = 1 index scan, không cần join |
| 3 | Nhóm theo `change_set_id` (UUID, 1 DB transaction), **không** có bảng header | Tránh FK/savepoint rollback; sau này partition được |
| 4 | **Tái dùng** `FieldSpec.history` + `FieldType.diff_repr()` đã có sẵn trong code | Không phát minh lại; cần hoàn thiện `diff_repr` cho number/temporal/relation |
| 5 | Actor đi qua `session.info` (late-bound từ request) + ContextVar cho CLI/job; **không FK** vào `auth_user` | Giữ đúng layering, history sống sót khi user bị xoá |
| 6 | Module mới `obox_builder.contrib.history`, đặt **ngay dưới `contrib.admin`** trong import-linter; model tự khai báo `__history__` (giống `__permissions__`) | `auth` không phải import `history` |
| 7 | Immutability bằng DB (trigger + REVOKE); tamper-evidence bằng *seal block* ở phase sau | Guardrail rẻ trước, bảo đảm mạnh sau |
| 8 | Rollout 4 phase; **Phase 1 (MVP)** = create/update/delete + timeline per-row cho `auth.User/Role` | Giá trị sớm, rủi ro thấp |

---

## 1. Bối cảnh: những gì code `dev` đã có / còn thiếu

**Đã có sẵn (tái dùng):**

- `FieldSpec.history: bool = True` (`fields/base.py`). `introspect.py` tự đặt `history=False` cho cột `secret` hoặc đuôi `_hash`; `history` là key được reserve trong `info={"admin": {...}}` (test: `token_hash.history is False`).
- `FieldType.diff_repr(value)` — docstring: *"Stable JSON representation used by the change log"*. Đã implement ở `Text`, `Choice`, `Boolean`, `Json`.
- `get_db()` = **1 transaction / request**, commit cuối; view `_create/_update/_delete` dùng `begin_nested()` + `flush()` (rollback savepoint khi `IntegrityError`).
- `RequestIDMiddleware` → `request.state.request_id`; `auth_session` lưu `ip`, `user_agent`.
- `post_migrate.send_async()` sau `upgrade` (auth dùng để `sync_permissions`) → tiền lệ để cài DDL/trigger sau migrate.
- `AppConfig.ready()`, `AppConfig.permissions` (permission cấp app), `Model.__permissions__` (permission cấp model), `core.signals.Signal`, hệ thống `checks` (`obox.admin.E101…`).
- `relationship()` mặc định `lazy="raise"` → listener **không được** trigger IO khi đọc attribute.
- Test: fixture `db` dùng `metadata.drop_all/create_all`; CI chạy **hai lượt** (SQLite và Postgres 17).

**Thiếu / rủi ro cần xử lý:**

- `diff_repr` **chưa** có cho `Integer`, `Decimal`, `Date`, `DateTime`, `Time`, `Link` → `datetime`/`Decimal` không JSON-serializable.
- **Nguồn noise:** `touch_session()` ghi `last_seen_at` mỗi request; `TimestampMixin.updated_at`; `User.last_login_at / failed_login_count / locked_until`.
- **M2M** (`auth_user_roles`) là bare `sa.Table`, ghi qua `target.roles = selected` (`views/users.py`) — không đổi cột nào của `User`.
- **Bulk Core** (`delete(AuthSession)…`) bỏ qua unit-of-work → không có flush event.
- README nhắc `docs/adr/` nhưng thư mục chưa tồn tại trên `dev` → đề xuất lưu tài liệu này thành `docs/adr/0001-row-history.md`.
- `BaseSettings` dùng `extra="forbid"` → mọi setting mới phải khai báo tường minh.

---

## 2. Nghiên cứu hệ thống production

### 2.1 Airtable — ba lớp tách biệt

1. **Record revision history (UI):** feed hoạt động trong record mở rộng. Retention theo plan: Business 2 năm, Enterprise Scale 3 năm; admin Enterprise chỉnh được tới 10 năm (bước 1 năm). *Clear revision history* là không khôi phục được. Field đồng bộ từ nguồn ngoài **bị ẩn** khỏi history; field ẩn trong interface thì thay đổi cũng không hiện (history **permission-aware**). Cộng đồng báo duplicate base / restore snapshot làm mất history và đổi record ID.
2. **Webhooks change feed (pull):** mỗi payload có `baseTransactionNumber`, `actionMetadata` (source + user), `changedRecordsById` với `current` / `previous` / `unchanged` (previous & unchanged phải opt-in), key theo *field ID*. Lấy bằng cursor; webhook hết hạn sau 7 ngày nếu không refresh; rate limit 5 req/s/base.
3. **Enterprise audit log:** events API (id, timestamp, action, actor, modelId, modelType, payload, **payloadVersion**, context, origin) và export NDJSON-gzip trong cửa sổ ≤180 ngày, link hết hạn 7 ngày.

Thêm: trash giữ mặc định 30 ngày; record tạo lại sau retention nhận **ID mới**.

### 2.2 AppSheet — "Audit History" *không phải* row history

- Audit History là **log vận hành** (sync, add/edit/delete, API call, bot invocation, report). Retention **7 ngày** (53 ngày với Enterprise Plus); ghi theo batch nên trễ tới \~5 phút; filter/analytics chỉ ở Enterprise; export BigQuery cho root admin. Cột *By User*: `-1` = guest, `-2` = chưa consent.
- Row history thật phải **tự dựng**: bot "data change event" + bảng Log, dùng `[_THISROW_BEFORE]` / `[_THISROW_AFTER]`, `USEREMAIL()`; hoặc dựa Change History của backend (Google Sheets, yêu cầu chạy "As App User").
- **Giới hạn quan trọng:** bot chỉ thấy before/after của đúng record kích hoạt; thay đổi **trực tiếp vào data source (ngoài app) không kích hoạt bot**; data-change do bot gây ra **không** kích hoạt bot khác (chống vòng lặp); điều kiện so sánh ô trống từng bắn nhầm (lỗi NULL/blank).

### 2.3 Jotform — ba lớp, nhưng thiếu field-level diff

- **Account Action Logs** (tab History): Submission Edits / Deletions / Purges, logins, API key… Enterprise admin console lọc theo user/time/action và export Excel/CSV.
- **Tables Revision History:** xem và *restore* bản trước của table (data, settings, visual); mọi lần restore **được log**.
- **Trash → Purge** hai pha (xoá mềm, rồi xoá vĩnh viễn là event riêng).
- **Hạn chế** (theo trả lời của support): Tables chỉ cho biết submission *đã được cập nhật* và thời điểm, **không** cho biết field nào/giá trị cũ; workaround là thêm field ẩn hoặc dựa email thông báo gắn tag "EDIT".

### 2.4 Tham chiếu bổ sung

| Hệ thống | Điểm đáng học |
| --- | --- |
| **Directus** | `directus_activity` (header) + `directus_revisions` (`data` snapshot + `delta` + `parent`). Issue #17894: \~700k revisions làm timeline rất chậm do thiếu index theo (collection, item) → bổ sung retention riêng cho activity/revisions. |
| **Salesforce** | Field history: giới hạn 20 field/object, giữ 18 tháng (UI) / 24 tháng (API); Field Audit Trail mở rộng retention tới 10 năm → *cap chi phí theo field* và *retention là tính năng hạng nhất*. |
| **django-auditlog** | `LogEntry`: `object_pk`, `object_repr`, `changes` (JSON), `actor`, `cid`, `remote_addr`, `timestamp`, `additional_data`; `mask_fields`; M2M không bật mặc định; `pre_log/post_log` signal; v3 đổi `changes` từ text sang JSON (→ cần versioning payload). |
| **supa\_audit** (Supabase) | Một bảng JSONB `record` + `old_record`, `record_id` ổn định từ PK, BRIN trên `ts`. **Repo đã archived (02/2025)** → chỉ tham khảo schema, không dùng làm dependency. |
| **SQLAlchemy** | Ví dụ `versioned_history` dùng `before_flush` + `attributes.get_history`; tài liệu asyncio cho phép bắt event trên `sync_session_class`. |

### 2.5 So sánh

|  | Field-level old→new | Actor + source | Nhóm theo transaction | Retention | Restore | Thay đổi ngoài app |
| --- | --- | --- | --- | --- | --- | --- |
| **Airtable** | Có (history + webhook `previous/current`) | Có (`actionMetadata.source`) | `baseTransactionNumber` | 2–3 năm, tới 10 năm | Rollback / snapshot | Field sync bị ẩn |
| **AppSheet** | Không có sẵn (DIY) | `By User`, -1/-2 | Không | 7 / 53 ngày | Không | **Không thấy** |
| **Jotform** | **Không** (chỉ "edited") | Có (action log) | Không | Theo plan | Table-level restore | — |
| **Directus** | Có (`delta`) | Có | `parent` | Cấu hình được | Revert | Không thấy |
| **Salesforce** | Có (Old/New) | Có | Không | 18–24 tháng; mở rộng được | Không | Không thấy |

### 2.6 Bài học → quyết định thiết kế

| # | Bài học (nguồn) | Quyết định |
| --- | --- | --- |
| L1 | Tách *audit hoạt động* khỏi *row history* (AppSheet, Airtable) | Bảng riêng chỉ cho data change; login/export event để phase sau |
| L2 | Phải lưu **old/new theo field** (Jotform thiếu) | `changes.fields = {f: [old, new]}` |
| L3 | Nhóm thao tác theo transaction (Airtable, Directus `parent`) | `change_set_id` |
| L4 | Nguồn + actor tường minh, có sentinel guest/system (Airtable `source`, AppSheet -1/-2) | `source`, `actor_id NULL`, `actor_label` |
| L5 | Blind spot khi ghi ngoài app (AppSheet) | Bulk guard + (Phase 4) DB trigger + `history_reconcile` |
| L6 | Chống đệ quy (bot không kích bot) | `RowChange` không được track; ghi bằng Core insert |
| L7 | So sánh null/blank sai → bắn nhầm (AppSheet) | So sánh có sentinel `NO_VALUE`, chuẩn hoá qua `diff_repr`; `""` ≠ `None` nếu type phân biệt |
| L8 | Index đúng từ đầu + keyset pagination (Directus #17894) | Index `(model_label, object_pk, id DESC)`; không dùng OFFSET |
| L9 | Retention hạng nhất, theo model (Airtable, Salesforce, Directus) | `HISTORY_RETENTION_DAYS` + override theo model; drop partition (Phase 4) |
| L10 | Read phải permission-aware (Airtable) | Lọc field theo `FieldSpec` + quyền của viewer |
| L11 | Restore là hành động *tiến*, được log, có conflict check (Jotform); ID tái dùng (Airtable) | `reverts_id`; UI chia "incarnation" tại `delete` |
| L12 | Payload có version (Airtable `payloadVersion`; auditlog v3) | cột `schema_version` |
| L13 | Cap chi phí (Salesforce 20 field) | `HISTORY_MAX_VALUE_BYTES`, allow-list, bỏ no-op |
| L14 | Đừng phụ thuộc lib bị archive (supa\_audit) | Tự cài đặt, chỉ mượn ý tưởng schema |
| L15 | Mask/redact dữ liệu nhạy cảm (auditlog `mask_fields`) | Marker `redacted`, tuỳ chọn `mask` |

---

## 3. Yêu cầu

**Chức năng**

- **F1** Ghi mọi INSERT/UPDATE/DELETE của model được track: actor, thời điểm, field old→new, source, request id, IP.
- **F2** Timeline per-row trong admin (kiểu activity feed của Airtable), phân trang keyset.
- **F3** Audit feed toàn cục, lọc theo model / actor / source / thời gian / `change_set`.
- **F4** Hiển thị *permission-aware*: viewer chỉ thấy field mình được phép thấy; secret không bao giờ lộ.
- **F5** Hoàn tác một thay đổi / khôi phục row đã xoá (Phase 3), luôn tạo entry mới.
- **F6** Export NDJSON/CSV, retention theo model, purge an toàn.

**Phi chức năng**

- **NF1 Atomic:** history commit ⇔ data commit (cùng transaction).
- **NF2 Overhead thấp:** 1 `executemany` / flush; không thêm SELECT cho update. *Ngân sách mục tiêu cần xác nhận bằng benchmark ở Phase 1, không phải số đã đo.*
- **NF3 Append-only** và về sau tamper-evident.
- **NF4 Portable:** SQLite (dev/test) + Postgres (prod); tính năng chỉ-Postgres phải tuỳ chọn.
- **NF5 Đúng layering** (import-linter).
- **NF6 Tiến hoá schema:** đổi tên/xoá/đổi kiểu cột không làm hỏng history cũ.
- **NF7 Quan sát được:** log, signal, check, lệnh CLI.

**Không nằm trong phạm vi v1:** comment trên row, ghi *đọc* (access log), history cho schema/config, tracking thay đổi của DB ngoài app, UI diff 3 chiều.

---

## 4. Kiến trúc

### 4.1 Component view

```
 HTTP request                                      CLI / background job
 ┌──────────────────────────────────────┐          ┌─────────────────────────┐
 │ RequestIDMiddleware                   │          │ async with history.use( │
 │ admin router: Depends(bind_history)   │          │   source="cli", ...)     │
 │   → db.info["obox.history.ctx"] = lazy│          │   → ContextVar          │
 └───────────────────┬──────────────────┘          └────────────┬────────────┘
                     ▼                                          ▼
        AsyncSession (get_db: 1 txn/request)            session_scope()
                     │  views / services: add, setattr, delete, flush
        ┌────────────▼─────────────────────────────────────────────┐
        │ contrib.history.capture (listeners trên sync Session)      │
        │  before_flush → tính Pending từ attribute history (no IO)  │
        │  after_flush  → 1× INSERT executemany (cùng transaction)   │
        └────────────┬─────────────────────────────────────────────┘
                     ▼
          history_row_change  (append-only, cùng commit với data)
                     ▲
        HistoryService (keyset, filter, che field theo quyền)
                     ▲
   admin: /<model>/{pk}/history · card trong form.html · /admin/history
```

### 4.2 Layering (import-linter)

```toml
layers = [
  "obox_builder.contrib.admin",
  "obox_builder.contrib.history",   # MỚI — admin import được, auth thì không cần
  "obox_builder.contrib.auth",
  "obox_builder.core", ... "obox_builder.db", "obox_builder.apps", "obox_builder.conf", "obox_builder.utils",
]
```

`history` chỉ import `db`, `fields`, `apps`, `core.signals/utils`, `conf` — tất cả ở tầng thấp hơn. Model khai báo declaratively, **không import**:

```python
class User(Model, TimestampMixin):
    __history__ = {"relations": ["roles"], "exclude": ["meta"]}
```

`Model` base thêm `__history__: ClassVar[bool | Mapping[str, Any] | None] = None` (cùng kiểu với `__permissions__`). `contrib.history` đọc ở `ready()`. Với model của bên thứ ba có API lập trình `history.track(Model, ...)` (giống `auditlog.register`).

### 4.3 Luồng ghi

1. `RequestIDMiddleware` gán `request_id`; router admin gắn *resolver* vào `db.info` (chưa biết user — late-binding).
2. Dependency xác thực điền `request.state.user`; view sửa object, gọi `flush()` trong `begin_nested()`.
3. **`before_flush`**: reset `session.info[PENDING]` (loại rác từ flush lỗi trước đó); duyệt `session.new / dirty / deleted`, với type được track thì tính `Pending` từ `attribute history` — *không* chạm attribute có thể lazy-load.
4. SQLAlchemy phát INSERT/UPDATE/DELETE → PK của row mới đã có.
5. **`after_flush`**: dựng rows, resolve context, `session.connection().execute(insert(RowChange), rows)` — một `executemany`.
6. Savepoint rollback (`IntegrityError` trong `_create`) kéo theo history cùng rollback; vì không có bảng header nên `change_set_id` luôn hợp lệ.
7. Commit cuối request → atomic. `post_record` signal gửi sau `after_commit`.

`change_set_id` sinh lười, lưu `session.info`, xoá ở `after_commit` / `after_rollback` của transaction ngoài cùng.

### 4.4 Design patterns áp dụng

| Pattern | Áp dụng | Vì sao |
| --- | --- | --- |
| **Observer** | Session events + `Signal` (`pre_record` / `post_record`) | Capture không sửa view/service |
| **Unit of Work** | `flush` là ranh giới batch + atomic | 1 insert/flush |
| **Strategy** | `FieldType.diff_repr()` | Đúng triết lý lõi của `fields/` |
| **Registry** | `trackers: dict[type, Tracker]` dựng ở `ready()` | Giống `site.register`, `field_types` |
| **Context Object + Null Object + Ambient Context** | `HistoryContext`; actor `system`; ContextVar | Không truyền actor qua mọi hàm |
| **Append-only event log** | `history_row_change` | Sự thật bất biến; suy ra state bằng reverse-apply |
| **Memento (reverse delta)** | Lưu `old` → khôi phục không cần snapshot | Hai chiều replay |
| **Command** | Revert = lệnh tường minh sinh thay đổi mới | Không bao giờ sửa lịch sử |
| **Repository / Query Object** | `HistoryService`, filter kiểu `QuerySpec` | Tách đọc khỏi UI |
| **Cursor-based pull / outbox** | `id` đơn điệu làm cursor cho shipper ngoài | Giống Airtable payload cursor |
| **Dependency inversion** | `__history__` khai báo thay vì import | Giữ layering |
| **Fail-closed / fail-open policy** | `HISTORY_FAILURE_MODE` | Compliance vs availability |

---

## 5. Database model

### 5.1 Tổng quan

Một bảng fact duy nhất, không FK ra ngoài, không bảng header.

```
history_row_change
  id            BIGINT identity PK     monotonic: thứ tự timeline + cursor phân trang
  occurred_at   timestamptz            UTC, do app set
  change_set_id uuid                   1 DB transaction (nhóm thao tác đa-row)
  op            create | update | delete
  model_label   'auth.user'   object_pk '42'   object_repr 'vinh'
  changes       jsonb (payload v1)     schema_version smallint
  row_version   int NULL               (nếu model có VersionedMixin)
  actor_id NULL · actor_label · source · request_id · ip · user_agent
  reason NULL · reverts_id NULL
```

### 5.2 Model (SQLAlchemy 2.0, theo convention hiện có)

```python
# obox_builder/contrib/history/models.py
class RowChange(Model):  # tablename suy ra: history_row_change
    __history__ = False  # recursion guard: không bao giờ track chính nó

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    change_set_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    op: Mapped[str] = mapped_column(sa.String(8))
    model_label: Mapped[str] = mapped_column(sa.String(150))
    object_pk: Mapped[str] = mapped_column(sa.String(255))
    object_repr: Mapped[str] = mapped_column(sa.String(255), default="")
    changes: Mapped[JsonDict] = mapped_column(JSONB)
    schema_version: Mapped[int] = mapped_column(sa.SmallInteger, default=1)
    row_version: Mapped[int | None]
    actor_id: Mapped[int | None] = mapped_column(sa.BigInteger)  # KHÔNG FK
    actor_label: Mapped[str] = mapped_column(sa.String(150), default="")
    source: Mapped[str] = mapped_column(sa.String(16))
    request_id: Mapped[str | None] = mapped_column(sa.String(64))
    ip: Mapped[str | None] = mapped_column(sa.String(45))
    user_agent: Mapped[str | None] = mapped_column(sa.String(255))
    reason: Mapped[str | None] = mapped_column(sa.String(500))
    reverts_id: Mapped[int | None] = mapped_column(sa.BigInteger)  # soft ref, không FK

    __table_args__ = (
        sa.CheckConstraint("op IN ('create','update','delete')", name="op"),
        sa.CheckConstraint(
            "source IN ('admin_ui','api','cli','system','import','revert','db_trigger')",
            name="source",
        ),
        sa.Index("ix_history_row_change_target", "model_label", "object_pk", sa.desc("id")),
        sa.Index("ix_history_row_change_actor", "actor_id", sa.desc("id")),
        sa.Index("ix_history_row_change_model", "model_label", sa.desc("id")),
        sa.Index("ix_history_row_change_change_set", "change_set_id"),
        sa.Index("ix_history_row_change_occurred_at", "occurred_at", postgresql_using="brin"),
    )
```

| Cột | Quyết định |
| --- | --- |
| `model_label` | Dùng đúng định danh sẵn có `ModelAdmin.model_label` (`app_label.model_name`) |
| `object_pk` | Text chuẩn hoá (int/str/UUID). Admin hiện chỉ hỗ trợ PK đơn (`primary_key[0]`); PK ghép → JSON array dạng text |
| `object_repr` | Snapshot `str(obj)`, fallback `#pk` (cùng quy ước `_object_label_for`) để row đã xoá vẫn đọc được |
| `actor_id` / `actor_label` | Không FK: không import `auth`, sống sót khi xoá user. `actor_label` = username tại thời điểm đó |
| `reverts_id` | Soft reference, không FK, để partition/purge không bị ràng buộc |
| `row_version` | Thứ tự per-row tuyệt đối nếu model dùng `VersionedMixin` (Phase 3), ngược lại NULL |

**Bốn bất biến giữ đường partition mở:** `occurred_at` luôn NOT NULL và do app set; không FK trỏ *vào* bảng này; không unique ngoài PK; mọi query timeline luôn mang `(model_label, object_pk)`.

### 5.3 Payload `changes` — schema v1

```json
{
  "fields":      {"status": ["draft", "published"], "title": ["Hello", "Hello world"]},
  "m2m":         {"roles": {"add": ["3"], "remove": ["1"]}},
  "redacted":    ["password_hash"],
  "truncated":   {"body": {"len": 120345, "sha256": "9f2c..."}},
  "unknown_old": []
}
```

- Key sắp xếp theo tên field (deterministic → golden test được). Giá trị qua `FieldType.diff_repr`.
- `create`: `[null, new]` cho mọi field non-null. `delete`: `[old, null]` cho mọi field non-null ⇒ **tombstone chính là snapshot đầy đủ cuối cùng**, không cần cột snapshot riêng. `update`: chỉ field có net change.
- Bỏ PK (đã nằm ở `object_pk`), bỏ field bị `exclude` hoặc `history=False`. Field `secret` chỉ vào `redacted` (biết *có* đổi, không biết giá trị).
- FK (`Link`): lưu giá trị cột FK thô, không lưu repr object liên quan.
- M2M: id dạng string (ổn định cho int/UUID), ghi vào row của phía *chủ động sửa*.
- Giá trị vượt `HISTORY_MAX_VALUE_BYTES`: lưu preview 256 ký tự + `{len, sha256}` trong `truncated`; entry đó **không revert được**.
- `unknown_old`: field đổi nhưng giá trị cũ chưa load (deferred/expired). Tuyệt đối không lazy-load trong listener.
- Thêm key mới là tương thích ngược; đổi nghĩa key cũ ⇒ tăng `schema_version`; reader hỗ trợ tối thiểu 2 version.

### 5.4 Index và dạng query

```sql
-- Timeline của 1 row: keyset, KHÔNG dùng OFFSET
SELECT * FROM history_row_change
WHERE model_label = :m AND object_pk = :pk AND id < :cursor
ORDER BY id DESC LIMIT 51;            -- 50 + 1 để biết còn trang sau

-- Feed toàn cục
... WHERE model_label = :m AND id < :cursor ORDER BY id DESC LIMIT 51;
... WHERE actor_id    = :a AND id < :cursor ORDER BY id DESC LIMIT 51;

-- Mọi thay đổi của một thao tác (1 request = 1 change_set)
... WHERE change_set_id = :cs ORDER BY id;
```

`request_id` dùng để đối chiếu log; muốn tra trực tiếp thì thêm partial index `WHERE request_id IS NOT NULL`. Chi phí ghi của 5 index trên bảng chỉ-chèn cần đo ở Phase 1; nếu write path nhạy cảm, bỏ index `model` (feed theo model sẽ lọc bằng BRIN + `model_label`).

### 5.5 Append-only (phòng thủ nhiều lớp)

1. **App:** chỉ INSERT bằng Core; không có đường UPDATE/DELETE trong code, ngoại trừ lệnh bảo trì.
2. **DB trigger**, cài bằng receiver `post_migrate` (cùng cơ chế `sync_permissions_receiver`, idempotent):

```sql
CREATE OR REPLACE FUNCTION history_forbid_mutation() RETURNS trigger AS $$
BEGIN
  IF current_setting('obox.history_maintenance', true) = 'on' THEN
    RETURN COALESCE(NEW, OLD);
  END IF;
  RAISE EXCEPTION 'history_row_change is append-only (%)', TG_OP USING ERRCODE = '42501';
END $$ LANGUAGE plpgsql;

CREATE TRIGGER history_row_change_immutable BEFORE UPDATE OR DELETE ON history_row_change
  FOR EACH ROW EXECUTE FUNCTION history_forbid_mutation();
CREATE TRIGGER history_row_change_no_truncate BEFORE TRUNCATE ON history_row_change
  FOR EACH STATEMENT EXECUTE FUNCTION history_forbid_mutation();
```

SQLite (dev/test): trigger chặn `UPDATE` bằng `RAISE(ABORT, ...)`; cho phép `DELETE` để purge ở môi trường dev. 3. **Role DB (ranh giới thật):** `GRANT INSERT, SELECT` cho role của app; `REVOKE UPDATE, DELETE, TRUNCATE`; role bảo trì riêng cho purge/anonymize.

> ⚠️ GUC `obox.history_maintenance` chỉ là guardrail chống tai nạn/bug: bất kỳ user nào có quyền `SET` đều bật được. Ranh giới bảo mật thật là `REVOKE` + role riêng. Chống superuser/DBA xấu cần hash-chain và neo ra ngoài (Phase 4).

### 5.6 Retention, partitioning, sizing

- **Retention:** `HISTORY_RETENTION_DAYS` toàn cục + override theo model qua `__history__["retention_days"]`. Lệnh `history_purge` xoá theo lô (≤10k dòng/lô, nghỉ giữa lô), bật `SET LOCAL obox.history_maintenance = 'on'`, và log mỗi lô ra logger (không ghi vào chính bảng).
- **Partitioning (Phase 4, chỉ Postgres):** RANGE theo tháng trên `occurred_at`, PK `(id, occurred_at)`, có partition `DEFAULT` để insert không bao giờ fail khi cron lỡ nhịp; retention = `DROP PARTITION` (O(1), không bloat). Chuyển bảng hiện hữu: tạo bảng partitioned mới rồi gắn bảng cũ làm partition đầu tiên (cần unique index chứa cột partition và CHECK range đã validate) hoặc backfill theo lô. Query timeline đi qua nhiều partition nhờ MergeAppend trên index `(model_label, object_pk, id DESC)` + `LIMIT` nên dừng sớm.
- **Sizing (giả định, KHÔNG phải số đo):** \~300 B cố định (cột + 5 index) + \~300 B payload ≈ 600 B/entry ⇒ 1M entry ≈ 0,6 GB; 50M ≈ 30 GB. Thay bằng số đo thật ở exit criteria của Phase 1.

### 5.7 Các hình dạng dữ liệu đã cân nhắc

| Phương án | Ưu | Nhược | Kết luận |
| --- | --- | --- | --- |
| Snapshot cả row mỗi version (SQLAlchemy `history_meta`, simple-history) | Restore dễ | Phình theo độ rộng row × số lần sửa | Không |
| EAV, 1 dòng/field (Salesforce) | Truy vấn và ACL theo field dễ | N dòng/lần save, mất tính nguyên tử của row | Không |
| **Delta JSONB / row** (auditlog, Directus `delta`) | Compact, 1 dòng/row/flush | Truy vấn theo field cần JSONB | **Chọn** |
| Delta + checkpoint snapshot định kỳ | Point-in-time nhanh | Phức tạp hơn | Để sau nếu cần (thêm cột nullable, rẻ trên Postgres) |

---

## 6. Capture engine

### 6.1 Tracker (resolve một lần ở `ready()`)

Nguồn field: `introspect_model(model, overrides=...)`, rồi lọc: bỏ PK; bỏ `FieldSpec.history is False` (không phải secret); bỏ cột `TimestampMixin` (`created_at`, `updated_at`); bỏ `__history__["exclude"]`; nếu có `__history__["fields"]` thì chỉ lấy allow-list. Field `secret` chỉ đưa vào `redacted`. Với `auth.User`, gắn `info={"admin": {"history": False}}` cho `last_login_at`, `failed_login_count`, `locked_until`, `password_changed_at` để khỏi nhiễu.

### 6.2 Pseudocode

```python
@event.listens_for(Session, "before_flush")
def before_flush(session, ctx, instances):
    pending = session.info[PENDING] = []  # reset: flush lỗi trước đó không để lại rác
    for obj in session.new:
        _add(pending, obj, "create")
    for obj in session.dirty:
        _add(pending, obj, "update")
    for obj in session.deleted:
        _add(pending, obj, "delete")


def _add(pending, obj, op):
    tracker = trackers.get(type(obj))
    if tracker is None:
        return
    change = tracker.diff(obj, op)  # chỉ đọc attribute history đã load, không IO
    if change.has_effect:  # no-op / chỉ field bị loại => bỏ
        pending.append(change)


@event.listens_for(Session, "after_flush")
def after_flush(session, ctx):
    pending = session.info.pop(PENDING, [])
    if not pending:
        return
    context = resolve_context(session)  # session.info -> ContextVar -> system
    cs = session.info.setdefault(CHANGE_SET, uuid4())
    rows = [c.to_row(context, cs) for c in pending]  # PK của row mới đã có sau flush
    session.connection().execute(sa.insert(RowChange), rows)  # 1 executemany, cùng transaction
```

- **Update:** với mỗi spec đọc `state.attrs[key].history` (property này *không* phát loader). `deleted` rỗng nghĩa là giá trị cũ chưa load ⇒ đưa vào `unknown_old`.
- **Delete:** dùng `state.attrs[key].loaded_value` (trả `NO_VALUE` nếu chưa load).
- **So sánh:** `diff_repr(old) != diff_repr(new)` với sentinel `NO_VALUE` (bài học L7, tránh lỗi blank=blank của AppSheet).
- **M2M:** với mỗi relationship trong `__history__["relations"]` có `secondary`, đọc `history.added / deleted` ⇒ `m2m[rel] = {add, remove}`. Collection phải được load trước khi gán (code `users.py` đã làm); **thêm test khoá hành vi này** vì `lazy="raise"`.
- Listener gắn vào class `sqlalchemy.orm.Session` (framework async-only nên đó chính là sync session bên dưới `AsyncSession`).

### 6.3 Ma trận edge case

| # | Tình huống | Hành vi | Giảm thiểu / test |
| --- | --- | --- | --- |
| 1 | Rollback hoặc savepoint rollback | Không có history (cùng transaction) | Test `_create` với `IntegrityError` |
| 2 | `update()`/`delete()` bulk (ORM-enabled) | Không qua unit-of-work ⇒ không có entry | Guard `do_orm_execute` theo `HISTORY_BULK_POLICY` (`ignore`/`warn`/`raise`); helper `history.bulk_update()` |
| 3 | SQL thô / psql / service khác | **Blind spot** (giống AppSheet) | Phase 4: trigger an toàn + `history_reconcile`; ghi rõ trong tài liệu |
| 4 | `ON DELETE CASCADE` ở DB | Row con không được ghi | `delete` của row cha ghi snapshot m2m đã load; hoặc cascade ở ORM cho model được track |
| 5 | Hai writer cùng sửa 1 row | `old` có thể cũ (lost update) | `VersionedMixin` (`version_id_col`) ⇒ `StaleDataError`; trigger DB cho độ chính xác tuyệt đối |
| 6 | Attribute deferred/expired | `unknown_old`, **không** lazy-load | Test với `lazy="raise"` |
| 7 | PK bị tái sử dụng (SQLite) | Timeline gộp hai "đời" | UI cắt đoạn tại `delete`; identity của Postgres không tái dùng |
| 8 | No-op save / chỉ đổi cột bị loại | Bỏ qua | Chống nhiễu `touch_session` |
| 9 | Giá trị khổng lồ | Truncate + sha256 | `HISTORY_MAX_VALUE_BYTES` |
| 10 | Field secret | Chỉ marker `redacted` | Test "không lộ giá trị" |
| 11 | `Decimal`, `datetime`, enum | Qua `diff_repr` | Hoàn thiện type còn thiếu ở Phase 1 |
| 12 | Cùng row flush 2 lần trong 1 transaction | 2 entry cùng `change_set` | UI gộp theo `change_set` |
| 13 | Đổi tên model | `model_label` cũ thành mồ côi | Lệnh bảo trì `history_relabel` |
| 14 | Xoá/đổi kiểu cột | Giữ tên cũ, UI gắn nhãn "legacy field" | `schema_version`, fallback `humanize(name)` |
| 15 | CLI / job không có request | ContextVar hoặc actor `system` | `history.use(...)` |
| 16 | Insert history lỗi | `closed`: fail cả transaction; `open`: savepoint + log + metric | `HISTORY_FAILURE_MODE` |

---

## 7. Truyền ngữ cảnh (actor, source)

```python
@dataclass(frozen=True, slots=True)
class HistoryContext:
    actor_id: int | None
    actor_label: str  # username | "system" | "anonymous" | "cli:<os-user>"
    source: str  # admin_ui | api | cli | system | import | revert | db_trigger
    request_id: str | None = None
    ip: str | None = None
    user_agent: str | None = None
    reason: str | None = None
    reverts_id: int | None = None
```

Thứ tự resolve: `session.info["obox.history.ctx"]` (callable late-bound) → ContextVar → `HistoryContext.system()`.

```python
# Web: router admin thêm dependency cạnh bind_site
async def bind_history(request: Request, db: AsyncSession = Depends(get_db)) -> None:
    history.bind(db, request)  # lưu closure; đọc request.state.user lúc flush


# CLI / job
async with history.use(source="cli", actor_label="cli:createsuperuser"):
    await services.create_user(...)
```

- **Late-binding là then chốt:** dependency cấp router chạy *trước* `permission_required` (nơi điền `request.state.user`), nên resolver phải đọc user ở thời điểm flush, không phải lúc bind.
- **IP:** chỉ tin `X-Forwarded-For` khi `HISTORY_TRUSTED_PROXIES` được cấu hình; mặc định dùng `request.client.host` (audit log không được tin header do client gửi).
- **Sentinel:** `actor_id = NULL` + `actor_label` ∈ {`system`, `anonymous`, `cli:<user>`} (học từ AppSheet `-1/-2`, nhưng không dùng số ma thuật).

---

## 8. Read path, UI và phân quyền

| Route | Mục đích | Quyền |
| --- | --- | --- |
| `GET /admin/<path>/{pk}/history` | Timeline đầy đủ, `?before=<id>` | `ModelAdmin.has_history_permission` |
| `GET /admin/history` | Feed toàn cục + filter | `history.view_row_change` |
| `GET /admin/history/{change_set_id}` | Chi tiết một thao tác | như trên |
| `POST /admin/<path>/{pk}/history/{entry_id}/revert` (P3) | Hoàn tác | `history.restore_row_change` + `change`/`add` của model |
| `GET /admin/history/export` (P3) | NDJSON / CSV | `history.export_row_change` |

- Route per-model đăng ký trong `register_model_routes` khi model được track.
- `has_history_permission(ctx, obj=None)` = `history.view_row_change` **và** `has_view_permission` (theo mẫu `has_*_permission` hiện có). Permission cấp app khai ở `HistoryConfig.permissions` (cơ chế `AppConfig.permissions` + `sync_permissions` đã có). Feed toàn cục chỉ hiện model mà viewer có quyền `view`.
- **UI:** cột phải của `form.html` (hiện chỉ có card nút) thêm card *Recent activity* (5 entry) + liên kết *View all*; chỉ hiện ở trang change, không hiện ở add. Mỗi entry: initials của actor, thời gian tương đối (tooltip UTC), badge `op` và `source`, danh sách `field: old → new`. Stack giữ nguyên: Jinja2 SSR + Tabler. Autoescape luôn bật, **cấm `|safe`** cho old/new (XSS từ dữ liệu đã lưu).
- **Che theo quyền (L10):** `visible = {spec.name for spec in form_specs + list_specs if not spec.secret}`; field ngoài tập này bị lọc khi render. `redacted` hiện là "đã thay đổi" không kèm giá trị. Field không còn trong spec (legacy) chỉ superuser thấy vì không biết nó có nhạy cảm hay không.
- `HistoryService` trả dataclass `HistoryItem` đã áp masking; template không bao giờ đọc JSON thô:

```python
recent(db, admin, pk, limit=5)
timeline(db, model_label, pk, before=None, limit=50)
feed(db, filters, before=None, limit=50)
change_set(db, change_set_id)
```

---

## 9. Revert / Restore (Phase 3)

Nguyên tắc: **không bao giờ sửa hay xoá entry cũ.** Revert là một thay đổi tiến, được log với `source='revert'` và `reverts_id` (Jotform cũng log mọi lần restore).

`revert(entry)`:

1. Kiểm tra quyền; chặn các field có `truncated`/`redacted`/`mask` và liệt kê rõ field nào không khôi phục được.
2. Trong một transaction: `SELECT ... FOR UPDATE` row hiện tại (Postgres; SQLite bỏ qua).
3. **Conflict check** từng field: `current == entry.new` thì cho qua, ngược lại trả 409 kèm `old / new / current` để người dùng chọn (3-way ở mức field).
4. Áp `old` qua đường chuẩn (`FieldType.from_input` + schema form) nên validator/constraint vẫn chạy; context `source=revert`.
5. Capture engine tự sinh entry mới `op=update, reverts_id=entry.id`.

Biến thể: revert `delete` = INSERT lại với PK gốc nếu PK chưa bị dùng (PK gốc luôn nhỏ hơn giá trị sequence hiện tại nên không va chạm), ghi `op=create`; revert `create` = delete có xác nhận; revert cả `change_set` = áp ngược theo `id DESC` trong một transaction, hoặc thất bại toàn bộ.

---

## 10. Bảo mật, riêng tư, tuân thủ

- **Secret** không bao giờ lưu giá trị, chỉ marker `redacted`.
- **PII:** `__history__["mask"]` (che một phần kiểu `a***@x.com`, field đó không revert được) hoặc `redacted` (chỉ marker); chọn theo field.
- **Erasure (GDPR):** `history_anonymize --actor-id N` hoặc `--model M --pk K` chạy bằng role bảo trì, thay `actor_label`/giá trị bằng token ẩn, **không xoá** entry (giữ cấu trúc timeline); mỗi lần chạy được log.
- **Truy cập:** phân quyền như mục 8; export có rate limit và log (logger `obox.history.audit`); CSV chống formula injection (tiền tố `'` cho ô bắt đầu bằng `=`, `+`, `-`, `@`).
- **Không tin dữ liệu do client gửi:** `RequestIDMiddleware` hiện nhận nguyên `x-request-id` từ client ⇒ phải chuẩn hoá về `[A-Za-z0-9._-]{1,64}` trước khi lưu, và coi đó là *gợi ý đối chiếu*, không phải bằng chứng. IP chỉ tin theo `HISTORY_TRUSTED_PROXIES`.
- **Mã hoá at-rest:** ở tầng DB/đĩa; history chứa bản sao dữ liệu nhạy cảm nên cần cùng mức bảo vệ với bảng gốc.
- **Tamper evidence (Phase 4):** bảng `history_seal(block_id, first_id, last_id, count, merkle_root, prev_block_hash, block_hash, sealed_at)`. Worker `history_seal` niêm phong các entry *cũ hơn một cửa sổ trễ* (lớn hơn `idle_in_transaction_session_timeout`/`statement_timeout`, ví dụ 10 phút) theo thứ tự `id`, để entry commit muộn không chèn vào khối đã seal; định kỳ neo `block_hash` ra nơi ngoài (object storage WORM / log service); `history_verify` kiểm tra lại. Entry vẫn bất biến, hash không ghi ngược vào entry.

---

## 11. Cấu hình, quan sát, kiểm tra hệ thống

**Settings** (thêm vào `BaseSettings`, prefix `OBOX_`; bắt buộc khai báo vì `extra="forbid"`):

| Setting | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `HISTORY_ENABLED` | `True` | Công tắc tổng |
| `HISTORY_FAILURE_MODE` | `"closed"` | `closed`: fail cả transaction; `open`: savepoint + log + tiếp tục |
| `HISTORY_BULK_POLICY` | `"warn"` | Bulk update/delete lên model được track: `ignore` / `warn` / `raise` |
| `HISTORY_MAX_VALUE_BYTES` | `8192` | Ngưỡng truncate mỗi giá trị |
| `HISTORY_RETENTION_DAYS` | `None` | `None` = giữ mãi |
| `HISTORY_TRUSTED_PROXIES` | `[]` | Danh sách proxy được tin `X-Forwarded-For` |
| `HISTORY_TRACK_ALL_ADMIN_MODELS` | `False` | Bật cho mọi model đã register trong `AdminSite` |

**Khai báo theo model:** `__history__ = True` hoặc `{"fields": [...], "exclude": [...], "relations": [...], "mask": [...], "retention_days": int}`.

**System checks** (cùng hệ `core/checks`): `obox.history.E201` PK ghép/không có PK; `E202` `relations` không phải relationship có `secondary`; `E203` field không tồn tại trong `fields`/`exclude`/`mask`; `W201` model được track nhưng không có `VersionedMixin` (rủi ro lost update); `W202` `HISTORY_FAILURE_MODE="open"`.

**Signals:** `pre_record`, `post_record` (đồng bộ, dùng `core.signals.Signal.send`; `post_record` bắn sau `after_commit`) là điểm cắm cho Prometheus/OTel và shipper SIEM. Metrics đề xuất: `history_entries_written_total{model,op}`, `history_write_seconds`, `history_failures_total`, `history_bulk_untracked_total`, `history_table_bytes`. Log: logger `obox.history`.

**CLI** (đặt trong `management/commands/` của app, như `sync_permissions`): `history_purge`, `history_export`, `history_anonymize`, `history_relabel`; Phase 4: `history_seal`, `history_verify`, `history_partition`, `history_reconcile`.

---

## 12. Chiến lược kiểm thử

Package `tests/history/`, dùng fixture `db` / `client` / `login`. CI đã chạy hai lượt (SQLite và Postgres 17) nên mọi test mặc định phải portable; test chỉ-Postgres gắn skip theo dialect.

| Nhóm | Nội dung |
| --- | --- |
| Unit (thuần) | Engine diff; `diff_repr` mọi `FieldType` (round-trip JSON); truncate/redact/mask; **golden file** payload v1 để đóng băng schema |
| Integration flush | create/update/delete; no-op; cột bị loại; M2M `roles`; model `__history__ = False` không bị track; `RowChange` không tự track |
| Atomicity | Rollback ⇒ 0 entry; `IntegrityError` trong `begin_nested()` ⇒ 0 entry và không để rác sang flush sau; `change_set_id` reset sau commit |
| Context | Web: `actor_id` đúng dù dependency router chạy trước auth (late-binding); CLI `history.use`; mặc định `system`; `request_id` được sanitize |
| Admin UI | Trang history render; 403 khi thiếu quyền; secret/field ẩn không lộ; XSS (`<script>` trong old/new bị escape); card trong form |
| Hiệu năng | Test đếm số câu SQL mỗi flush = đúng +1 insert (không N+1); benchmark có marker, không chạy mặc định |
| Chỉ-Postgres | Trigger append-only; concurrency hai session cùng row (có/không `VersionedMixin`); partition (P4) |
| Kiến trúc | `lint-imports` với layer mới; `ruff`; `pyright`; `mkdocs build --strict` |

---

## 13. Roadmap

Ước lượng thô cho 1 dev quen codebase, chỉ để sắp thứ tự, không phải cam kết.

| Phase | Nội dung | Exit criteria | Cỡ |
| --- | --- | --- | --- |
| **0** | ADR `docs/adr/0001-row-history.md`; spike listener + benchmark insert 1k/10k row, đo overhead và kích thước entry | Số đo thay các giả định ở 5.6 và NF2 | \~0,5 tuần |
| **1 — MVP** | App `contrib.history`; `RowChange` + migration; hoàn thiện `diff_repr`; capture create/update/delete (chưa M2M); context (session.info + ContextVar + admin bind); `Model.__history__`; track `auth.User/Role`; settings, checks; trang timeline + card trong form; docs | CI xanh trên SQLite + PG; test không lộ secret; test atomicity; overhead nằm trong ngân sách đã đo | \~1,5–2 tuần |
| **2** | M2M; bulk guard + helper; feed toàn cục + filter; masking theo quyền; trigger immutability (`post_migrate`); signals; `history_purge`, `history_export`; retention | Feed đúng quyền; purge theo lô an toàn; trigger chặn UPDATE | \~1–1,5 tuần |
| **3** | Revert/restore (kể cả khôi phục row đã xoá); `VersionedMixin`; export UI; `mask`; `history_anonymize` | Conflict check đúng; revert luôn sinh entry mới | \~1,5 tuần |
| **4** | Hardening Postgres: partitioning + partition DEFAULT; trigger lưới an toàn + `history_reconcile`; seal block + `history_verify`; tài liệu REVOKE/role; `history_relabel` | Retention bằng DROP PARTITION; verify phát hiện sửa/xoá | \~2 tuần |

**Danh sách file thay đổi ở Phase 1:**

```
obox_builder/contrib/history/{__init__,apps,models,options,tracker,capture,context,payload,service,checks,signals}.py
obox_builder/db/base.py                    + Model.__history__ ClassVar
obox_builder/fields/types/{number,temporal,relation}.py    + diff_repr
obox_builder/contrib/auth/models.py        + __history__ cho User/Role; info history=False cho cột gây nhiễu
obox_builder/contrib/admin/{options,urls}.py, views/history.py (mới), views/model.py (route + card)
obox_builder/contrib/admin/templates/admin/history/*.html, model/form.html (card), navigation.py (mục "Audit log")
obox_builder/conf/base.py                  + HISTORY_* settings
pyproject.toml                             + importlinter layer
docs/history.md · docs/adr/0001-row-history.md · mkdocs.yml · CHANGELOG.md
tests/history/...
```

---

## 14. Quyết định đã cân nhắc (ADR tóm tắt)

| Chủ đề | Chọn | Loại bỏ | Vì sao |
| --- | --- | --- | --- |
| Capture | SQLAlchemy Session events | DB trigger; CDC (logical decoding/Debezium); override `save()` | Chỉ app biết actor, request và `FieldType`; portable; trigger giữ làm lưới an toàn ở P4; CDC quá nặng cho admin nội bộ |
| Thư viện | Tự cài đặt | sqlalchemy-continuum (bảng shadow theo model); supa\_audit (archived, chỉ Postgres) | Shadow table nhân đôi schema/migration; cần khớp `FieldType`, async, `lazy="raise"` |
| Nhóm thao tác | `change_set_id` denormalized | Bảng header (kiểu Directus `activity`) | Tránh FK/savepoint, dễ partition; chi phí lặp metadata nhỏ |
| Actor | `actor_id` + `actor_label` snapshot | FK `auth_user` | Giữ layering, sống sót khi xoá user; đổi lại erasure cần lệnh bảo trì |
| Partition | Hoãn tới Phase 4 | Làm ngay ngày 1 | PK ghép + Identity không portable sang SQLite; bốn bất biến giữ đường mở |
| Mặc định track | Opt-in theo model (`__history__`) | Track tất cả | Kiểm soát dung lượng (bài học Salesforce); `auth` luôn bật |

---

## 15. Rủi ro và câu hỏi mở

**Rủi ro**

1. **Blind spot khi ghi ngoài app** (SQL thô, service khác, bulk): giảm bằng guard + Phase 4, nhưng không thể loại bỏ hoàn toàn ở tầng app.
2. **`old` sai khi hai writer đồng thời** nếu model không có `VersionedMixin` (edge #5).
3. **Lỗi trong listener làm hỏng flush:** mặc định fail-closed; cần test kỹ và có `HISTORY_FAILURE_MODE`.
4. **Ngữ nghĩa attribute history của SQLAlchemy** với collection/replace/deferred: phải khoá bằng test ở Phase 1 trên cả SQLite và PG.
5. **Tăng trưởng không giới hạn:** retention + (P4) partition; theo dõi `history_table_bytes`.
6. **`RequestIDMiddleware` tin `x-request-id` từ client:** phải sanitize trước khi lưu.

**Câu hỏi cần bạn chốt** (ảnh hưởng thứ tự ưu tiên)

1. Mức tuân thủ: audit nội bộ hay môi trường có quy định? (quyết định độ ưu tiên của immutability/hash-chain)
2. Model nào phải track ngay khi ra mắt: chỉ `auth`, hay mọi model admin?
3. Revert/restore có bắt buộc ở v1 không?
4. Yêu cầu retention: 1 năm, 3 năm, hay vô thời hạn?
5. DB production chỉ Postgres? (mở khoá partition và trigger)
6. Có writer ngoài admin (API công khai, worker, service khác) không? (quyết định độ ưu tiên của trigger lưới an toàn)

---

## Nguồn

- **Airtable:** record revision history; data retention policies; Webhooks API (payload, expiration); audit log events API — support.airtable.com, airtable.com/developers/web/api
- **AppSheet:** Monitor app activity using Audit History (support.google.com/appsheet/answer/10104794); Bots, Events và data change, `_THISROW_BEFORE/AFTER` (answers 11432969, 11547057, 11445188)
- **Jotform:** account activity logs (jotform.com/help/370); Tables revision history (jotform.com/help/1011); Enterprise admin console; forum "Tables: Viewing Specific Data Updates and Revision History for Submissions"
- **Directus:** schema docs, revisions reference, issue #17894, PR #24058 (retention)
- **Salesforce:** Field History Tracking / Field Audit Trail (Trailhead, SalesforceBen)
- **django-auditlog** docs (usage, internals); Supabase blog "Postgres Auditing in 150 lines of SQL" và github.com/supabase/supa\_audit (archived)
- **SQLAlchemy 2.0:** ORM events (`before_flush`/`after_flush`), asyncio events, `examples/versioned_history`
- **Mã nguồn:** github.com/vinhgiga/obox-builder, branch `dev` @ `dabd36d`
