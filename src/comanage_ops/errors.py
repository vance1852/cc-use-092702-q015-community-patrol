"""共管任务与补偿核算服务向 API 和 CLI 暴露的稳定错误。"""


class ComanageError(RuntimeError):
    code = "comanage_error"
    status = 400


class NotFound(ComanageError):
    code = "not_found"
    status = 404


class Conflict(ComanageError):
    code = "conflict"
    status = 409


class Forbidden(ComanageError):
    code = "forbidden"
    status = 403


class InvalidState(ComanageError):
    code = "invalid_state"
    status = 409


class ValidationFailed(ComanageError):
    code = "validation_failed"
    status = 422
