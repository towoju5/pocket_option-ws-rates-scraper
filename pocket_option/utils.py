from __future__ import annotations

import contextlib
import datetime
import inspect
import random
import time
import typing

import pytz

from pocket_option.constants import TIMESTAMP_OFFSET

if typing.TYPE_CHECKING:
    from collections import deque

    from pocket_option.types import JsonFunction, JsonValue

__all__ = (
    "append_or_replace",
    "fix_timestamp",
    "generate_index",
    "generate_request_id",
    "get_json_function",
    "set_pretty_name",
)

rnd = random.SystemRandom()


@typing.runtime_checkable
class _FnClsWithPretty(typing.Protocol):
    __pretty_name__: str


def set_pretty_name[T](item: T, name: str) -> T:
    item.__pretty_name__ = name  # type: ignore
    return item


def get_function_full_name(fn: typing.Callable) -> str:
    if isinstance(fn, _FnClsWithPretty):
        return fn.__pretty_name__
    if inspect.isclass(fn):
        return fn.__name__ + ".__init__"
    if fn.__module__ and hasattr(fn, "__qualname__"):
        return f"{fn.__module__}.{fn.__qualname__}"
    if hasattr(fn, "__qualname__"):
        return fn.__qualname__
    return repr(fn)  # type: ignore


def get_json_function() -> JsonFunction:
    with contextlib.suppress(ImportError):
        import ujson  # type: ignore  # noqa: PLC0415

        class _UJson:
            def loads(self, value: str | bytes) -> JsonValue:
                return ujson.loads(value)

            def dumps(self, value: JsonValue, *, separators: tuple[str, str] | None = None) -> str:
                return ujson.dumps(value, ensure_ascii=False, separators=separators)

        return _UJson()

    import json  # noqa: PLC0415

    class _JsonLoads:
        def loads(self, value: str | bytes) -> JsonValue:
            return json.loads(value)

        def dumps(self, value: JsonValue, *, separators: tuple[str, str] | None = None) -> str:
            return json.dumps(value, ensure_ascii=False, separators=separators)

    return _JsonLoads()


@typing.overload
def append_or_replace[T](
    array: list[T],
    item: T,
    eq_by_keys: list[str],
    get_key_method: typing.Callable[[T, str], typing.Any] = getattr,
) -> list[T]: ...
@typing.overload
def append_or_replace[T](
    array: deque[T],
    item: T,
    eq_by_keys: list[str],
    get_key_method: typing.Callable[[T, str], typing.Any] = getattr,
) -> deque[T]: ...
def append_or_replace[T](
    array: list[T] | deque[T],
    item: T,
    eq_by_keys: list[str],
    get_key_method: typing.Callable[[T, str], typing.Any] = getattr,
) -> list[T] | deque[T]:
    for i, it in enumerate(array):
        if all(get_key_method(it, key) == get_key_method(item, key) for key in eq_by_keys):
            array[i] = item
            return array
    array.append(item)
    return array


def get_server_time() -> float:
    return time.time() - TIMESTAMP_OFFSET


@typing.overload
def fix_timestamp(ts: float) -> float: ...
@typing.overload
def fix_timestamp(ts: datetime.datetime) -> datetime.datetime: ...
def fix_timestamp(ts: typing.Any) -> typing.Any:
    if isinstance(ts, float):
        return ts + TIMESTAMP_OFFSET
    if isinstance(ts, datetime.datetime):
        return datetime.datetime.fromtimestamp(ts.timestamp() + TIMESTAMP_OFFSET, tz=pytz.UTC)
    raise TypeError(f"Unsupported type: {type(ts)}")


def generate_request_id() -> int:
    return int(get_server_time()) + rnd.randint(1, 100)


def generate_index() -> int:
    return int(f"{int(get_server_time())}{rnd.randint(1, 100)}")
