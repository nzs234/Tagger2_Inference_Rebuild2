"""Shared error type for the tag manager modules."""

from __future__ import annotations


class TagManagerError(RuntimeError):
    def __init__(self, message: str, *, code: str, status_code: int = 400, retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


__all__ = ["TagManagerError"]
