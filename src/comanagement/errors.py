"""社区共管服务向 API 和 CLI 暴露的稳定错误。"""


class ComanagementError(RuntimeError):
    code = "comanagement_error"
    status = 400


class NotFound(ComanagementError):
    code = "not_found"
    status = 404


class Conflict(ComanagementError):
    code = "conflict"
    status = 409


class Forbidden(ComanagementError):
    code = "forbidden"
    status = 403


class InvalidState(ComanagementError):
    code = "invalid_state"
    status = 409


class ValidationFailed(ComanagementError):
    code = "validation_failed"
    status = 422
