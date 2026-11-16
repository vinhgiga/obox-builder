from typing import Any


class APIException(Exception):
    status_code = 500
    default_detail = "A server error occurred."

    def __init__(self, detail: Any = None, code: str | None = None) -> None:
        self.detail = self.default_detail if detail is None else detail
        self.code = code
        super().__init__(self.detail)


class ValidationError(APIException):
    status_code = 400
    default_detail = "Invalid input."

    def __init__(self, detail: Any = None, code: str | None = None) -> None:
        if detail is not None and not isinstance(detail, (dict, list)):
            detail = [detail]
        super().__init__(detail, code)


class ParseError(APIException):
    status_code = 400
    default_detail = "Malformed request."


class AuthenticationFailed(APIException):
    status_code = 401
    default_detail = "Incorrect authentication credentials."


class NotAuthenticated(APIException):
    status_code = 401
    default_detail = "Authentication credentials were not provided."


class PermissionDenied(APIException):
    status_code = 403
    default_detail = "You do not have permission to perform this action."


class NotFound(APIException):
    status_code = 404
    default_detail = "Not found."


class MethodNotAllowed(APIException):
    status_code = 405

    def __init__(self, method: str) -> None:
        super().__init__(f'Method "{method}" not allowed.')


class NotAcceptable(APIException):
    status_code = 406
    default_detail = "Could not satisfy the request Accept header."


class UnsupportedMediaType(APIException):
    status_code = 415
