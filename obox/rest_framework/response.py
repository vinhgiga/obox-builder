from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse


class Response(JSONResponse):
    def __init__(
        self,
        data: Any = None,
        status: int = 200,
        headers: Any = None,
        content_type: str | None = None,
    ) -> None:
        self.data = data
        super().__init__(
            jsonable_encoder(data), status_code=status, headers=headers, media_type=content_type
        )
        if status == 204:
            self.body = b""
