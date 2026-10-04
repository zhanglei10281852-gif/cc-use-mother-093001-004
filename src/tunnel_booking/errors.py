"""业务错误类型。"""


class BookingError(Exception):
    """所有可预期业务错误的基类。"""

    code = "BOOKING_ERROR"


class NotFoundError(BookingError):
    code = "NOT_FOUND"


class InvalidStateError(BookingError):
    code = "INVALID_STATE"


class PermissionDeniedError(BookingError):
    code = "PERMISSION_DENIED"


class ApprovalExpiredError(BookingError):
    code = "APPROVAL_EXPIRED"


class OccupancyLockedError(BookingError):
    """撤场验收完成前尝试释放占位。"""

    code = "OCCUPANCY_LOCKED"


class IdempotencyConflictError(BookingError):
    code = "IDEMPOTENCY_CONFLICT"


class ValidationErrorCode(BookingError):
    code = "VALIDATION_ERROR"
