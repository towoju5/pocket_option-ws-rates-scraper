from __future__ import annotations

import contextlib
import enum
import logging
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

_logger = logging.getLogger("pocket_option.middlewares")


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
        if event in ("updateStream", "updateAssets", "chafor") and not isinstance(data, list):
            # The upstream server occasionally sends a malformed/unexpected payload shape
            # for these events (e.g. a raw string instead of a list of tuples). Skip it
            # instead of raising per-item below: with WEBAPP_ALWAYS_ON_ASSETS=all this can
            # otherwise fire hundreds of times per burst (once per subscribed asset, once
            # per registered handler), and logger.exception's traceback formatting for that
            # many exceptions synchronously stalls the whole event loop.
            _logger.warning("Ignoring malformed %r payload: %r", event, data)
            return []
        if event == "updateStream":
            items = []
            for it in typing.cast("list", data):
                if not isinstance(it, (list, tuple)) or len(it) < 3:
                    _logger.warning("Ignoring malformed updateStream item: %r", it)
                    continue
                items.append({"asset": it[0], "timestamp": fix_timestamp(it[1]), "value": it[2]})
            return items
        if event == "updateAssets":
            items = []
            for it in typing.cast("list", data):
                if not isinstance(it, (list, tuple)) or len(it) != len(UPDATE_ASSETS_KEYS):
                    _logger.warning("Ignoring malformed updateAssets item: %r", it)
                    continue
                items.append(dict(zip(UPDATE_ASSETS_KEYS, it, strict=True)))
            return items
        if event == "chafor":
            items = []
            for it in typing.cast("list", data):
                if not isinstance(it, (list, tuple)) or len(it) != 2:
                    _logger.warning("Ignoring malformed chafor item: %r", it)
                    continue
                items.append(dict(zip(["asset", "value"], it, strict=True)))
            return items

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
