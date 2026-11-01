"""LUT 库和拟合共用的用户错误。"""

from typing import Any


class LutError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: dict[Any, Any] | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra or {}
