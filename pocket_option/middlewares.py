from __future__ import annotations

import contextlib
import enum
import typing

import pydantic

from pocket_option.middleware import Middleware
from pocket_option.utils import fix_timestamp, get_json_function

if typing.TYPE_CHECKING:
    from pocket_option.types import EmitCallback, JsonFunction, JsonValue


__all__ = (
    "FixTypesMiddleware",
    "MakeJsonOnMiddleware",
)


UPDATE_ASSETS_KEYS: typing.Final[list[str]] = [
    "id",
    "asset",
    "label",
    "type",
    "digits",
    "payout",
    "default_expiration",
    "min_expiration",
    "expiration_step",
    "is_otc",
    "otc_id",
    "real_id",
    "signals",
    "exp_time",
    "active",
    "timeframes",
    "scheduled_until",
    "min_quick_timeframe",
    "scheduled_at",
]


@typing.runtime_checkable
class _HasSpecDump(typing.Protocol):
    def __spec_dump__(self) -> JsonValue: ...


class MakeJsonOnMiddleware(Middleware):
    def __init__(self, json: JsonFunction | None = None) -> None:
        self.json = json or get_json_function()

    async def on(self, event: str, data: str | bytes | JsonValue | None) -> JsonValue | None:  # noqa: ARG002
        if isinstance(data, str | bytes):
            with contextlib.suppress(Exception):
                return self.json.loads(data)
        return typing.cast("JsonValue", data)


class FixTypesMiddleware(Middleware):
    async def on(self, event: str, data: JsonValue | None) -> JsonValue | None:  # type: ignore
        if data is None:
            return None
        if event == "updateStream":
            return [
                {
                    "asset": it[0],
                    "timestamp": fix_timestamp(it[1]),
                    "value": it[2],
                }
                for it in typing.cast("list[tuple[str, float, float]]", data)
            ]
        if event == "updateAssets":
            return [dict(zip(UPDATE_ASSETS_KEYS, it, strict=True)) for it in typing.cast("list[list]", data)]
        if event == "chafor":
            return [dict(zip(["asset", "value"], it, strict=True)) for it in typing.cast("list[list]", data)]

        return data

    @classmethod
    def _make_data(cls, v: typing.Any) -> typing.Any:
        if isinstance(v, _HasSpecDump):
            return v.__spec_dump__()
        if isinstance(v, dict):
            return {d_k: cls._make_data(d_v) for d_k, d_v in v}
        if isinstance(v, (list, tuple, set)):
            return [cls._make_data(it) for it in v]
        if isinstance(v, enum.Enum):
            return v.value
        if isinstance(v, pydantic.BaseModel):
            return v.model_dump(mode="json", by_alias=True)
        return v

    async def emit(
        self,
        event: str,
        data: JsonValue | None = None,
        callback: EmitCallback[JsonValue] | None = None,
    ) -> tuple[str, JsonValue | None, EmitCallback[JsonValue] | None]:
        data = self._make_data(data)
        return event, data, callback
