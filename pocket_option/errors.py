from __future__ import annotations

import typing

__all__ = ("DealError", "PocketOptionError")


class PocketOptionError(Exception):
    def __init__(self, code: str, message: str, extras: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extras = extras

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"

    def with_extras(self, **extras: typing.Any) -> typing.Self:
        """Return a new instance of DealError with updated extras."""
        return self.__class__(self.code, self.message, extras)


class DealError(PocketOptionError): ...
