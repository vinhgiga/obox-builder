# Đánh giá và đề xuất refactor REST API của Obox

Khảo sát ngày 19/11/2026 tại commit `181bad2`. Phạm vi là API JSON mà lập trình viên ứng dụng viết trên Obox. Đề xuất cho phép thay đổi Python API, HTTP contract và cách cấu hình, theo yêu cầu bỏ tương thích ngược.

## Quyết định

**Giữ CRUD sinh tự động. Thay bốn lớp view và `SimpleRouter` bằng một hàm `model_router()` trả về `fastapi.APIRouter`.** API nghiệp vụ dùng `APIRouter`, kiểu Pydantic và dependency FastAPI trực tiếp. Obox cung cấp thêm dependency cho danh tính, quyền và transaction, dựa trên runtime hiện có. Số lớp public riêng của Obox ở bề mặt định tuyến đề xuất là **0**.

Giá trị của API hiện tại đến từ hai cơ chế. `ModelViewSet` sinh các thao tác lặp lại, còn `ModelSerializer` suy ra schema từ model SQLAlchemy. Bỏ cơ chế thứ nhất làm CRUD dài lên rõ rệt. Bỏ luôn việc suy ra schema thì gộp các lớp thành một factory vẫn giảm khái niệm phải học, nhưng ví dụ có thể dài hơn Obox hiện tại. Hai cơ chế này có thể dùng ngay với route và schema native. [Ví dụ CRUD hiện tại](examples/apis/query-resource.md) · [FastAPI APIRouter](https://fastapi.tiangolo.com/reference/apirouter/) · [Pydantic create_model](https://docs.pydantic.dev/latest/concepts/models/#dynamic-model-creation)

## Đo chi phí code ứng dụng

Mẫu là `Product` với khóa số nguyên `id` và `name` bắt buộc, tối đa 50 ký tự. Đọc chỉ trả `id` và `name`. Ghi chỉ nhận `name`. CRUD cơ bản gồm list, retrieve, create, update, delete. Update dùng PATCH ở FastAPI và kiến trúc đề xuất, PUT ở Flask-AppBuilder. Obox hiện có cả hai. Case cơ bản đo khai báo cho 5 thao tác logic. Case phân quyền bên dưới đo code bổ sung. Số dòng được đếm sau `ruff format --line-length 88`, gồm import và gắn router, bỏ dòng trắng và dòng chỉ có comment. Model ORM, cấu hình ứng dụng, Alembic và session dùng chung đã có sẵn. Phụ lục ghi đầy đủ các đoạn code được đếm.

| Mẫu                                  | Dòng code | Chi phí ứng dụng                                                              |
| ------------------------------------- | ---------: | -------------------------------------------------------------------------------- |
| Obox hiện tại                       |         16 | Hai lớp, một truy vấn và ba dòng đăng ký URL.                            |
| FastAPI thuần                        |         50 | Ba schema có kế thừa, năm handler và một hàm tra bản ghi dùng chung.    |
| Flask-AppBuilder 5.1                  |         13 | Một lớp, projection đọc/ghi theo từng thao tác, một lời gọi đăng ký. |
| Đề xuất, sinh schema từ allowlist |         11 | Một lời gọi khai báo model, field đọc/ghi, thao tác và họ quyền.       |
| Đề xuất, Pydantic khai báo tay    |         27 | Cùng factory và ba schema rõ ràng cho một hợp đồng riêng.               |

Các hàng so sánh cùng công việc ứng dụng, với khác biệt về mã lỗi, envelope, xác thực mặc định và các tính năng đi kèm. Mẫu tuyệt đối ngắn nhất của Flask-AppBuilder bỏ khai báo projection theo từng thao tác và dựa vào suy luận mặc định. Bảng tính cả projection vì đây là hợp đồng ứng dụng cần duy trì khi model được thêm cột. Hàng Obox cơ bản dùng mặc định `AllowAny`, hàng đề xuất đã tính cấu hình họ quyền. [Flask-AppBuilder Model REST API](https://flask-appbuilder.readthedocs.io/en/latest/rest_api.html#model-rest-api) · [Mặc định Obox](https://github.com/vinhgiga/obox-builder/blob/181bad295e2f808a58edad17acf4e5f673856c29/obox/rest_framework/settings.py)

Với **CRUD theo chủ sở hữu**, Obox hiện tại cần **23 dòng** (thêm 7 dòng). Đề xuất cần **20 dòng** (thêm 9 dòng), trên router đã có quyền theo thao tác. Callback typed tốn thêm hai dòng ở phần tùy biến, còn tổng code giảm nhờ phần khai báo chung. Hai cách đều phải viết điều kiện SQL để giới hạn list, detail, update và delete, rồi tự gán owner từ principal khi tạo. Payload chỉ nhận `name`.

Một endpoint báo giá có path ID, body và response typed, cùng quyền riêng, cần **18 dòng** theo đề xuất và **19 dòng** khi dùng ngay FastAPI trong Obox hiện tại. Chênh lệch là một dòng import. Khi viết qua `APIView`, lập trình viên phải tự nối parsing/validation với request, trả response và khai báo tài liệu riêng nếu cần OpenAPI đầy đủ. Với một endpoint GET đơn giản, kể cả việc đăng ký, phép đếm là **8 dòng** qua `APIView` và **6 dòng** qua FastAPI.

## Đối chiếu framework và hệ thống thực tế

| Nguồn                               | Cách làm đã xác minh                                                                                                                                                                                                                                                                       | Hàm ý cho Obox                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| FastAPI và template chính thức    | `APIRouter` nhận dependency ở mức router hoặc route, suy ra body/response và đưa vào OpenAPI. Template viết handler CRUD và điều kiện owner bằng SQL trực tiếp, với schema tạo/cập nhật/đọc.                                                                            | Dùng chính chữ ký native cho endpoint riêng. Viết CRUD tay giữ được kiểm soát nhưng phải lặp các thao tác. [FastAPI Bigger Applications](https://fastapi.tiangolo.com/tutorial/bigger-applications/) · [Template CRUD](https://github.com/fastapi/full-stack-fastapi-template/blob/master/backend/app/api/routes/items.py)                                                                                                                                                                   |
| Flask-AppBuilder và Apache Superset | `BaseApi` gom route Flask qua `@expose`. `ModelRestApi` dùng `SQLAInterface`, Marshmallow và sinh CRUD, OpenAPI, quyền theo thao tác. Superset đăng ký `ChartRestApi` và `DashboardRestApi` thật, với schema, filter, soft delete và handler riêng khi có nghiệp vụ. | Mô hình suy ra CRUD từ ORM giảm code thật. Với FastAPI, việc gom route tùy chỉnh đã có `APIRouter`. Ví dụ Superset cho thấy phần nghiệp vụ phức tạp vẫn cần cấu hình và code riêng. [FAB REST API](https://flask-appbuilder.readthedocs.io/en/latest/rest_api.html) · [Đăng ký API Superset](https://github.com/apache/superset/blob/master/superset/initialization/__init__.py) · [Superset Chart API](https://github.com/apache/superset/blob/master/superset/charts/api.py) |
| Django REST Framework 3.16.1         | Serializer, viewset và router giúp khai báo CRUD gọn trong hệ sinh thái Django. Quyền từng object và lọc queryset là hai bước riêng.                                                                                                                                              | Suy ra schema, CRUD và policy theo queryset đáng giữ. Dispatch, routing và validation kiểu DRF thêm một đường xử lý song song với FastAPI. [DRF ViewSets](https://www.django-rest-framework.org/api-guide/viewsets/) · [DRF Permissions](https://www.django-rest-framework.org/api-guide/permissions/)                                                                                                                                                                                          |
| FastCRUD                             | `crud_router()` nhận model SQLAlchemy async, schema Pydantic, allowlist thao tác và dependencies, rồi tạo route FastAPI.                                                                                                                                                                 | Xác nhận bề mặt factory khả thi trên cùng stack. Ưu tiên adapter mỏng dùng metadata và UoW của Obox. Việc tích hợp FastCRUD cần kiểm chứng riêng cách dùng chung transaction, quyền và quyền theo hàng. [FastCRUD Router](https://benavlabs.github.io/fastcrud/api/crud_router/)                                                                                                                                                                                                    |
| Litestar và Advanced Alchemy        | Repository/service chia sẻ truy vấn và ghi dữ liệu. Tích hợp FastAPI dùng lifespan, dependency và session SQLAlchemy.                                                                                                                                                                  | Nếu code CRUD sinh ra và route custom thực sự lặp cùng persistence logic, dùng hàm nội bộ chung. Chưa có nhu cầu bắt ứng dụng tự khai báo repository. [Advanced Alchemy Framework Integrations](https://advanced-alchemy.litestar.dev/latest/usage/frameworks/index.html)                                                                                                                                                                                                                   |
| Apache Airflow 3                     | Route DAG runs dùng FastAPI, dependency và schema riêng cho nghiệp vụ. Airflow 3 chuyển FAB Auth Manager sang provider tùy chọn.                                                                                                                                                        | Ví dụ triển khai workflow API typed và quyền dưới dạng dependency. [Airflow DAG runs](https://github.com/apache/airflow/blob/main/airflow-core/src/airflow/api_fastapi/core_api/routes/public/dag_run.py) · [Airflow 3 release notes](https://airflow.apache.org/docs/apache-airflow/stable/release_notes.html#airflow-3-0-0-2025-04-22)                                                                                                                                                              |

## Obox hiện tại mang lại gì

| Thành phần                                      | Giá trị thực tế                                                                                                                                                      | Quyết định                                                                                                                                     |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `APIView`, `Response`, API exceptions         | Quản lý instance theo request, JSON, xác thực và định dạng lỗi kiểu DRF. `APIView.as_view()` chỉ đưa `Request` vào chữ ký FastAPI.                   | Dùng chữ ký FastAPI, dependency và cơ chế lỗi Obox hiện có. Gỡ lớp view và response kiểu DRF.                                        |
| `GenericAPIView`                                | Gom lookup, serializer, filter và pagination. `get_queryset()` là hook có ích để giới hạn hàng.                                                               | Đưa xử lý chung vào router factory. Cho phép truyền điều kiện scope bằng callback.                                                     |
| `GenericViewSet`, `SimpleRouter`, `@action` | Gom action cùng model và sinh URL. Với API đọc riêng, lập trình viên tự khai báo action hoặc tái sử dụng method của `ModelViewSet`.                    | Factory đăng ký chính xác các thao tác được chọn. Dùng decorator FastAPI cho action nghiệp vụ.                                      |
| `ModelViewSet`                                  | Sinh sáu thao tác, xử lý status và có các hook lưu.                                                                                                              | Giữ sinh CRUD trong factory. Route tùy biến có thể dùng cùng context và transaction.                                                      |
| `Serializer`, `ModelSerializer`, REST fields  | Khai báo projection ngắn, suy ra kiểu, chuẩn hóa đầu vào, kiểm tra unique/FK và xử lý file. Serializer thường còn là bộ khai báo validation song song. | Dùng Pydantic cho hợp đồng HTTP. Tái sử dụng metadata SQLAlchemy sẵn có. Giữ kiểm tra liên quan DB và file tại tầng ghi dữ liệu. |
| Auth, permission, filter, pagination kiểu DRF    | Quyền và query là giá trị thật, trong khi app phải biết thêm `REST_FRAMEWORK`, chuỗi import và các lớp adapter.                                           | Tái dùng Auth/RBAC runtime, khai báo quyền trên route, cấu hình query bằng field và giới hạn thay vì subclass.                        |

Probe tại local xác nhận OpenAPI của `POST /api/items/` có status 201 nhưng thiếu `requestBody` và schema JSON response là `{}`. `GET /api/items/{pk}/` thiếu mô tả tham số path. Endpoint FastAPI có chữ ký typed trong cùng môi trường đưa cả model request, model response và kiểu ID vào OpenAPI. Lý do trực tiếp là wrapper chỉ nhận `Request`, đọc JSON bằng tay và `_mount()` đặt `response_model=None`. [APIView](https://github.com/vinhgiga/obox-builder/blob/181bad295e2f808a58edad17acf4e5f673856c29/obox/rest_framework/views.py) · [URL adapter](https://github.com/vinhgiga/obox-builder/blob/181bad295e2f808a58edad17acf4e5f673856c29/obox/urls.py) · [FastAPI Response Model](https://fastapi.tiangolo.com/tutorial/response-model/)

`AppConfig.routers` đã được `Obox` chuyển vào runtime để mount dưới `/api/{app_label}`. Runtime đã phát hiện `api_context()` trong cây dependency để đăng ký quyền. `_Runtime.uow()` quản lý session/commit/rollback, còn `db._fields` đã tạo được Pydantic model từ metadata SQLAlchemy. Đây là điểm tái sử dụng trực tiếp. Đường REST hiện tại có session adapter riêng qua generic HTML view. [Obox adapter](https://github.com/vinhgiga/obox-builder/blob/181bad295e2f808a58edad17acf4e5f673856c29/obox/application.py) · [API context](https://github.com/vinhgiga/obox-builder/blob/181bad295e2f808a58edad17acf4e5f673856c29/obox/_dependencies.py) · [Runtime và UoW](https://github.com/vinhgiga/obox-builder/blob/181bad295e2f808a58edad17acf4e5f673856c29/obox/_runtime.py) · [Field metadata](https://github.com/vinhgiga/obox-builder/blob/181bad295e2f808a58edad17acf4e5f673856c29/obox/db/_fields.py)

Chạy bộ kiểm thử hiện có trên FastAPI 0.143.3, Pydantic 2.14.1 và SQLAlchemy 2.0.55 cho kết quả **336 đạt, 5 bỏ qua**. Hai ca đối chiếu DRF trong nhóm bỏ qua đã chạy lại và đạt với Django 5.2.18 và DRF 3.16.1 trong môi trường riêng. Ba ca còn lại cần PostgreSQL, pandas hoặc polars. [Kiểm thử REST](https://github.com/vinhgiga/obox-builder/blob/181bad295e2f808a58edad17acf4e5f673856c29/tests/test_rest.py) · [Kiểm thử đối chiếu](https://github.com/vinhgiga/obox-builder/blob/181bad295e2f808a58edad17acf4e5f673856c29/tests/test_reference_contracts.py)

Mẫu FastAPI ở phụ lục đã chạy đủ năm thao tác trên SQLite và xác nhận PATCH bỏ field khác với gửi `null`. Với Flask-AppBuilder 5.1.0, đăng ký mẫu trên app SQLite sinh đúng năm thao tác và schema đọc `id/name`, ghi `name`. Probe riêng dựng model từ `db._fields.schema()` rồi gắn vào một route FastAPI. OpenAPI nhận cả hai schema, response loại thuộc tính nội bộ, còn input quá dài hoặc có field lạ trả 422. Hai con số của factory là số đo bản thiết kế, sẽ cần đo lại khi triển khai.

## Kiến trúc đích

**`obox.api.model_router(...)`** cấu hình tại startup, trả về một `APIRouter` có route typed cho từng thao tác được chọn. Mỗi thao tác gắn tên quyền cụ thể như `catalog.products.retrieve`. `operations` được khai báo rõ. Nếu sinh schema, dùng `read_fields` và `write_fields` riêng. Việc công khai một field để đọc không tự cấp quyền ghi field đó. Với hợp đồng khác, dùng `read_schema`, `create_schema` và `update_schema` Pydantic. Nếu cần cấu trúc dữ liệu hoặc workflow phức tạp, dùng handler FastAPI và domain service.

- **Routing.** Gắn router qua `AppConfig.routers` hoặc public hóa phương thức `Obox.include_router()` đã có ở runtime. Dùng `app.include_router()` cho route FastAPI độc lập. Với router cần `api_context()`, dùng cơ chế mount của Obox để bơm dependency và đăng ký quyền. Tên path/operation ID ổn định, schema name duy nhất theo resource và thao tác. Dùng converter/kiểu ID theo metadata được hỗ trợ. Phát hiện trùng đường dẫn và cấu hình sai tại startup.
- **Schema.** Build Pydantic model một lần khi tạo router, rồi đưa kiểu thực vào chữ ký FastAPI. Tách projection đọc, create và PATCH. PATCH dùng `model_dump(exclude_unset=True)`, phân biệt bỏ field với gửi `null`. Đánh dấu field đọc bắt buộc theo hợp đồng response, độc lập với default của DB. Từ chối field ngoài allowlist và field ẩn, kể cả khi tính năng sinh schema được bật. SQLAlchemy model vẫn có thể thay đổi độc lập với HTTP contract. [FastAPI Body Updates](https://fastapi.tiangolo.com/tutorial/body-updates/) · [Mã sinh schema hiện có](https://github.com/vinhgiga/obox-builder/blob/181bad295e2f808a58edad17acf4e5f673856c29/obox/db/_fields.py)
- **Quyền.** Tái dùng `api_context` cho danh tính active và quyền mỗi thao tác. Đưa owner/tenant vào cùng truy vấn cho list, count và lookup trên các thao tác record. Truy vấn ngoài phạm vi trả 404. Tạo mới gán khóa scope ở server. Với cookie session, đưa kiểm tra CSRF trên các thao tác ghi vào dependency dùng chung. Bearer phải kiểm quyền lẫn scope token. Probe source xác nhận `api_context()` hiện chỉ kiểm danh tính/quyền, còn các route contrib gọi CSRF riêng. [API context](https://github.com/vinhgiga/obox-builder/blob/181bad295e2f808a58edad17acf4e5f673856c29/obox/_dependencies.py) · [Identity và token scope](https://github.com/vinhgiga/obox-builder/blob/181bad295e2f808a58edad17acf4e5f673856c29/obox/_auth.py) · [Ví dụ CSRF hiện tại](https://github.com/vinhgiga/obox-builder/blob/181bad295e2f808a58edad17acf4e5f673856c29/obox/contrib/files/routes.py)
- **Transaction.** Một request dùng một UoW. Flush/refresh đủ dữ liệu để dựng response, commit trước khi gửi thành công qua dependency `scope="function"`; lỗi rollback qua UoW. DB vẫn là nguồn quyết định cho unique/FK khi có ghi đồng thời. Thống nhất lỗi xung đột 409 và validation 422 cho API mới, cập nhật OpenAPI và error envelope theo cơ chế Problem hiện có. [FastAPI dependency scope](https://fastapi.tiangolo.com/tutorial/dependencies/dependencies-with-yield/#early-exit-and-scope) · [Obox UoW](https://github.com/vinhgiga/obox-builder/blob/181bad295e2f808a58edad17acf4e5f673856c29/obox/_runtime.py) · [Problem và ApiRoute](https://github.com/vinhgiga/obox-builder/blob/181bad295e2f808a58edad17acf4e5f673856c29/obox/_routes.py)
- **List.** Dùng giới hạn trang hữu hạn, sắp xếp ổn định có khóa phụ là PK, chỉ lọc/sắp xếp qua field được phép. Với CRUD cơ bản, dùng query đơn giản. Với use case phức tạp, xét dùng lại parser và query helpers đã có, theo đúng phạm vi cần thiết. [Query helpers](https://github.com/vinhgiga/obox-builder/blob/181bad295e2f808a58edad17acf4e5f673856c29/obox/_query.py)

HTTP contract đề xuất gồm GET collection/detail, POST 201, PATCH 200, DELETE 204, 422 cho input sai và 409 cho xung đột khi ghi. Dùng URL không có slash cuối như các route JSON contrib. List trả `{items, page}` với `limit`, `offset` và `hasMore` trong `page`. Chỉ tính tổng khi có nhu cầu. PUT toàn phần chỉ dùng ở endpoint có nhu cầu. Thiết kế v1 lấy model SQLAlchemy có một PK, field scalar và projection rõ ràng làm phạm vi. Các quan hệ, import/batch, thao tác nhiều bảng và nghiệp vụ versioning dùng handler riêng hoặc extension có caller cụ thể. [Phạm vi REST hiện tại](api-spec.md) · [Route JSON contrib hiện tại](https://github.com/vinhgiga/obox-builder/blob/181bad295e2f808a58edad17acf4e5f673856c29/obox/contrib/auth/api.py)

## Trình tự thay đổi và tiêu chí nghiệm thu

1. Public hóa context/dependency cần thiết và hoàn thành một lát cắt `Product` gồm CRUD, một scope owner và một custom route typed. Xác nhận từ chữ ký handler đến OpenAPI, quyền và UoW trước khi chuyển bề mặt API.
2. Chuyển ví dụ CRUD, action và cấu hình auth sang API mới. Đưa việc kiểm tra DB và claim file vào dịch vụ ghi dùng chung. Một route nhận attachment dùng schema ID và gọi dịch vụ đó trong cùng UoW. Hiện `obox.contrib.files.serializers.AttachmentField` import REST `JSONField` và `ModelSerializer` gọi `Files.resolve()`. Chuyển các caller này trước khi bỏ gói. [File serializer](https://github.com/vinhgiga/obox-builder/blob/181bad295e2f808a58edad17acf4e5f673856c29/obox/contrib/files/serializers.py) · [File claim/resolve](https://github.com/vinhgiga/obox-builder/blob/181bad295e2f808a58edad17acf4e5f673856c29/obox/contrib/files/__init__.py)
3. Gỡ `obox.rest_framework`, `REST_FRAMEWORK` và nhánh dispatch REST trong `obox.urls`. Rà lại tài liệu, các ví dụ và kiểm thử contract DRF liên quan đến REST. `path/include` còn được view HTML dùng. Tài liệu cũ có nhắc `obox.contrib.resources`, nhưng checkout khảo sát thiếu source package đó, nên kế hoạch không lấy package này làm dependency. [URL adapter](https://github.com/vinhgiga/obox-builder/blob/181bad295e2f808a58edad17acf4e5f673856c29/obox/urls.py) · [Python API hiện hành](reference/python-api.md)
4. Xác minh trên các route đã chuyển. Kiểm schema body/path/response, thao tác bị tắt không có route, scope với list/count/lookup, owner do server gán, CSRF cho cookie, scope cho bearer, phân biệt PATCH bỏ field và gửi null, rollback khi ghi lỗi và claim file hoạt động trong cùng UoW. Dùng PostgreSQL cho ca unique đồng thời. Lặp lại phép đếm LOC ở phụ lục khi factory thực sự chạy.

Tiêu chí quyết định là giảm code của CRUD thông thường so với Obox hiện tại, khi giữ projection rõ ràng và quyền đã khai báo. Với Pydantic viết tay, chấp nhận phần dòng schema tăng nếu app cần hợp đồng độc lập. Endpoint nghiệp vụ typed giữ nguyên cách viết FastAPI. Số dòng là số đo boilerplate ứng dụng, chưa phản ánh hiệu năng, chất lượng runtime hoặc chi phí cho mọi domain.

## Phụ lục đo dòng

### A1. Obox hiện tại

```python
from sqlalchemy import select

from app.models import Product
from obox.rest_framework.routers import SimpleRouter
from obox.rest_framework.serializers import ModelSerializer
from obox.rest_framework.viewsets import ModelViewSet
from obox.urls import include, path


class ProductSerializer(ModelSerializer):
    class Meta:
        model = Product
        fields = ("id", "name")


class Products(ModelViewSet):
    queryset = select(Product)
    serializer_class = ProductSerializer


router = SimpleRouter()
router.register("products", Products)
urlpatterns = [path("api/catalog/", include(router.urls))]
```

### A2. Flask-AppBuilder

```python
from flask_appbuilder.api import ModelRestApi
from flask_appbuilder.models.sqla.interface import SQLAInterface

from app import appbuilder
from app.models import Product


class Products(ModelRestApi):
    resource_name = "products"
    datamodel = SQLAInterface(Product)
    list_columns = ["id", "name"]
    show_columns = ["id", "name"]
    add_columns = ["name"]
    edit_columns = ["name"]
    exclude_route_methods = ("info",)


appbuilder.add_api(Products)
```

### A3. Factory đề xuất với schema sinh tự động

`app` đã được tạo. Ví dụ dùng một dòng mount qua adapter đề xuất `obox.include_router(router, prefix="/api/catalog")`. Trong app đã có `AppConfig`, thay dòng mount bằng `routers = (router,)` trên config.

```python
from app.models import Product
from obox.api import model_router

router = model_router(
    Product,
    prefix="/products",
    read_fields=("id", "name"),
    write_fields=("name",),
    operations=("list", "retrieve", "create", "update", "delete"),
    permission_prefix="catalog.products",
)
obox.include_router(router, prefix="/api/catalog")
```

### A4. Các schema Pydantic dùng chung cho hai mẫu tiếp theo

`ProductPatch.name` có giá trị mặc định để chấp nhận payload bỏ field. Chỉ dữ liệu gửi lên được lấy qua `exclude_unset=True`. Với field `name` không nullable, Pydantic từ chối `null` và chuỗi rỗng gửi tường minh.

```python
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

ProductName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)
]


class ProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: ProductName


class ProductPatch(ProductCreate):
    name: ProductName = ""


class ProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: ProductName
```

### A5. Factory đề xuất với schema khai báo tay

Tổng số dòng gồm A4 và đoạn sau, tính như hai module ứng dụng.

```python
from app.models import Product
from app.schemas import ProductCreate, ProductPatch, ProductRead
from obox.api import model_router

router = model_router(
    Product,
    prefix="/products",
    read_schema=ProductRead,
    create_schema=ProductCreate,
    update_schema=ProductPatch,
    operations=("list", "retrieve", "create", "update", "delete"),
    permission_prefix="catalog.products",
)
obox.include_router(router, prefix="/api/catalog")
```

### A6. FastAPI thuần

Tổng số dòng gồm A4 và đoạn sau. `DatabaseSession` là kiểu dependency của ứng dụng đã dùng chung cho các API, có transaction kết thúc trước khi gửi response. Mẫu này không tính quyền chi tiết theo thao tác, filter, pagination hoặc endpoint khác.

```python
from fastapi import APIRouter, HTTPException, Response
from sqlalchemy import select

from app.db import DatabaseSession
from app.models import Product
from app.schemas import ProductCreate, ProductPatch, ProductRead

router = APIRouter(prefix="/products")


async def get_product(product_id: int, session: DatabaseSession) -> Product:
    if (product := await session.get(Product, product_id)) is None:
        raise HTTPException(404, "Product not found")
    return product


@router.get("", response_model=list[ProductRead])
async def list_products(session: DatabaseSession) -> list[Product]:
    return list(await session.scalars(select(Product).order_by(Product.id)))


@router.get("/{product_id}", response_model=ProductRead)
async def read_product(product_id: int, session: DatabaseSession) -> Product:
    return await get_product(product_id, session)


@router.post("", status_code=201, response_model=ProductRead)
async def create_product(payload: ProductCreate, session: DatabaseSession) -> Product:
    product = Product(**payload.model_dump())
    session.add(product)
    await session.flush()
    return product


@router.patch("/{product_id}", response_model=ProductRead)
async def update_product(
    product_id: int, payload: ProductPatch, session: DatabaseSession
) -> Product:
    product = await get_product(product_id, session)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(product, field, value)
    await session.flush()
    return product


@router.delete("/{product_id}", status_code=204)
async def delete_product(product_id: int, session: DatabaseSession) -> Response:
    await session.delete(await get_product(product_id, session))
    return Response(status_code=204)


app.include_router(router, prefix="/api/catalog")
```

### B1. CRUD theo owner trên Obox hiện tại

Thêm hai import sau và thay lớp `Products` của A1. `owner_id` chỉ tồn tại trên ORM, ngoài projection. Tính số dòng tăng thêm so với A1.

```python
from typing import Any

from obox.rest_framework.permissions import IsAuthenticated


class Products(ModelViewSet):
    queryset = select(Product)
    serializer_class = ProductSerializer
    permission_classes = (IsAuthenticated,)

    def get_queryset(self) -> Any:
        return super().get_queryset().where(Product.owner_id == self.request.user.id)

    async def perform_create(self, serializer: ModelSerializer) -> None:
        await serializer.save(owner_id=self.request.user.id)
```

### B2. CRUD theo owner với factory đề xuất

Thêm các import và hai hàm sau, rồi thêm `scope=product_scope,` và `create_values=set_owner,` vào A3. Factory tự áp điều kiện scope ở mọi lookup và dùng giá trị tạo từ server sau khi validate payload.

```python
from typing import Any

from sqlalchemy import Select

from obox.api import Context


def product_scope(ctx: Context, stmt: Select[tuple[Product]]) -> Select[tuple[Product]]:
    return stmt.where(Product.owner_id == ctx.principal.id)


def set_owner(ctx: Context, values: dict[str, Any]) -> dict[str, Any]:
    return {**values, "owner_id": ctx.principal.id}
```

### C. Một endpoint typed với quyền riêng

Ví dụ dùng một service báo giá có sẵn. Service đọc sản phẩm theo phạm vi của người dùng. Đo toàn bộ code biên giới HTTP cho endpoint. Khi dùng Obox hiện tại, import `Context` và `api_context` từ hai module nội bộ và dùng một dòng mount qua `AppConfig.routers` hoặc runtime. Module public đề xuất gộp được hai import thành một dòng.

```python
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, PositiveInt

from app.services import price_quote
from obox.api import Context, api_context

router = APIRouter(prefix="/products")
QuoteContext = Annotated[Context, Depends(api_context(permission="catalog.products.quote"))]


class QuoteIn(BaseModel):
    quantity: PositiveInt


class QuoteOut(BaseModel):
    total: Decimal


@router.post("/{product_id}/quote")
async def quote(product_id: int, body: QuoteIn, ctx: QuoteContext) -> QuoteOut:
    return QuoteOut(total=await price_quote(ctx, product_id, body.quantity))


obox.include_router(router, prefix="/api/catalog")
```

### D1. GET độc lập qua APIView hiện tại

```python
from fastapi import Request

from obox.rest_framework.response import Response
from obox.rest_framework.views import APIView
from obox.urls import path


class Health(APIView):
    def get(self, request: Request) -> Response:
        return Response({"status": "ok"})


urlpatterns = [path("api/health", Health.as_view())]
```

### D2. GET độc lập qua FastAPI

```python
from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(router, prefix="/api")
```
