from __future__ import annotations

import abc
import collections.abc
import contextlib
import datetime
import math
import os
import pathlib
import typing
import warnings
from collections import defaultdict, deque
from itertools import chain

import pydantic
import pytz

from pocket_option.generated_client import PocketOptionClient
from pocket_option.models import Asset, LoadHistoryPeriodFastResponse, UpdateCloseValueItem
from pocket_option.utils import append_or_replace

if typing.TYPE_CHECKING:
    from pocket_option.generated_client import PocketOptionClient

__all__ = ("Candle", "CandleStorage", "MemoryCandleStorage", "RedisCandleStorage")


class Candle(pydantic.BaseModel):
    asset: Asset
    timestamp: datetime.datetime
    timeframe: int
    open: float
    low: float
    high: float
    close: float


class CandleStorage(abc.ABC):
    """
    Abstract candle storage.

    CandleStorage stores raw price updates and provides OHLC candle
    aggregation.

    The storage subscribes to PocketOption close value updates and converts
    incoming price events into stored items.

    Raw price updates are represented by UpdateCloseValueItem.
    Candles are generated dynamically using timeframe buckets.

    Example:

        candles = await storage.get_candles(
            Asset.AUDCAD_otc,
            timeframe=60,
            count=100,
        )

    """

    def __init__(self, client: PocketOptionClient) -> None:
        self.client = client

        self.client.on.update_close_value(self._on_update_close_value)
        self.client.on.load_history_period_fast(self._on_load_history_period_fast)

        self.client.candles = self

    async def _on_load_history_period_fast(self, data: LoadHistoryPeriodFastResponse) -> None:
        await self.add_item_bulk(
            list(
                chain.from_iterable(
                    [
                        [
                            UpdateCloseValueItem(asset=data.asset, timestamp=it.time, value=it.open),
                            UpdateCloseValueItem(
                                asset=data.asset,
                                timestamp=it.time + 0.01,
                                value=it.low,
                            ),
                            UpdateCloseValueItem(
                                asset=data.asset,
                                timestamp=it.time + 0.02,
                                value=it.high,
                            ),
                            UpdateCloseValueItem(
                                asset=data.asset,
                                timestamp=it.time + (data.period - 0.01),
                                value=it.close,
                            ),
                        ]
                        for it in data.data
                    ],
                ),
            ),
        )

    async def _on_update_close_value(self, items: list[UpdateCloseValueItem]) -> None:
        await self.add_item_bulk(items)

    async def add_candle(self, candle: Candle) -> None:
        """
        Add complete candle into storage.

        The candle is converted into synthetic price updates:

        - open  -> timestamp + 0.00
        - low   -> timestamp + 0.01
        - high  -> timestamp + 0.02
        - close -> timestamp + timeframe - 0.01

        This allows candle reconstruction through the same aggregation
        mechanism used for live market data.
        """
        await self.add_item_bulk(
            [
                UpdateCloseValueItem(asset=candle.asset, timestamp=candle.timestamp.timestamp(), value=candle.open),
                UpdateCloseValueItem(
                    asset=candle.asset,
                    timestamp=candle.timestamp.timestamp() + 0.01,
                    value=candle.low,
                ),
                UpdateCloseValueItem(
                    asset=candle.asset,
                    timestamp=candle.timestamp.timestamp() + 0.02,
                    value=candle.high,
                ),
                UpdateCloseValueItem(
                    asset=candle.asset,
                    timestamp=candle.timestamp.timestamp() + (candle.timeframe - 0.01),
                    value=candle.close,
                ),
            ],
        )

    @abc.abstractmethod
    async def add_item(self, item: UpdateCloseValueItem):
        """
        Store a single price update.

        Must be implemented by subclasses.
        """

    @abc.abstractmethod
    async def add_item_bulk(self, items: list[UpdateCloseValueItem]):
        """
        Store multiple price updates.

        Must be implemented by subclasses.
        """

    @abc.abstractmethod
    async def get_items(
        self,
        asset: Asset,
        *,
        start: datetime.datetime | None = None,
        end: datetime.datetime | None = None,
        count: int | None = None,
    ) -> collections.abc.Iterable[UpdateCloseValueItem]:
        """
        Retrieve raw price updates.

        :param asset: Asset to query.
        :type asset: Asset

        :param start: Minimum timestamp filter.
        :type start: datetime.datetime | None

        :param end: Maximum timestamp filter.
        :type end: datetime.datetime | None

        :param count: Maximum number of latest items.
        :type count: int | None

        :return: Iterable of price updates ordered by timestamp.
        :rtype: collections.abc.Iterable[UpdateCloseValueItem]
        """

    @abc.abstractmethod
    async def get_first_item(self, asset: Asset) -> UpdateCloseValueItem | None:
        """
        Retrieve the first price update for the given asset.

        :param asset: Asset to query.
        :type asset: Asset

        :return: First price update or None if no data exists.
        :rtype: UpdateCloseValueItem | None
        """

    async def get_candles(
        self,
        asset: Asset,
        timeframe: int = 5,
        *,
        start: datetime.datetime | None = None,
        end: datetime.datetime | None = None,
        count: int | None = None,
    ) -> collections.abc.Iterable[Candle]:
        """
        Build OHLC candles from stored price updates.

        Price updates are grouped into timeframe buckets:

            bucket = floor(timestamp / timeframe) * timeframe

        For each bucket:

            open  = first price
            close = last price
            high  = maximum price
            low   = minimum price


        :param asset: Asset to build candles for.
        :type asset: Asset

        :param timeframe: Candle size in seconds.
        :type timeframe: int

        :param start: Start datetime filter.
        :type start: datetime.datetime | None

        :param end: End datetime filter.
        :type end: datetime.datetime | None

        :param count: Number of latest candles.
        :type count: int | None

        :return: Iterable of generated candles.
        :rtype: collections.abc.Iterable[Candle]
        """
        items = await self.get_items(asset, start=start, end=end, count=count)

        buckets: dict[int, list[UpdateCloseValueItem]] = defaultdict(list)
        for item in items:
            ts_bucket = math.floor(item.timestamp / timeframe) * timeframe
            buckets[ts_bucket].append(item)
        candles = []

        for ts_bucket in sorted(buckets):
            group = buckets[ts_bucket]
            values = [i.value for i in group]
            candle = Candle(
                asset=asset,
                timestamp=datetime.datetime.fromtimestamp(ts_bucket, tz=pytz.UTC),
                timeframe=timeframe,
                open=values[0],
                close=values[-1],
                high=max(values),
                low=min(values),
            )
            candles.append(candle)

        return candles


class MemoryCandleStorage(CandleStorage):
    """
    In-memory candle storage.

    Stores raw price updates in memory using bounded deques.

    Features:
        - separate storage per asset;
        - automatic replacement of duplicate timestamps;
        - configurable maximum history size.

    Data is lost after process restart.

    Example:

        storage = MemoryCandleStorage(client)
        ...
        candles = await storage.get_candles(
            Asset.AUDCAD_otc,
            timeframe=60,
        )
    """

    def __init__(self, client: PocketOptionClient) -> None:
        super().__init__(client)
        self._max_len = 10_000
        self._storage: dict[Asset, deque[UpdateCloseValueItem]] = defaultdict(lambda: deque(maxlen=10_000))

    def set_max_len(self, _max_len: int):
        """
        Change maximum number of stored price updates per asset.


        :param max_len: Maximum deque size.
        :type max_len: int
        """
        old_storage = self._storage.copy()
        self._storage = defaultdict(lambda: deque(maxlen=_max_len))
        for key, old_deque in old_storage.items():
            self._storage[key].extend(old_deque)

    async def get_first_item(self, asset: Asset) -> UpdateCloseValueItem | None:
        items = self._storage.get(asset, [])
        if not items:
            return None
        with contextlib.suppress(ValueError):
            return min(items, key=lambda i: i.timestamp)
        return None

    async def add_item(self, item: UpdateCloseValueItem):
        self._storage[item.asset] = append_or_replace(self._storage[item.asset], item, ["asset", "timestamp"])

    async def add_item_bulk(self, items: list[UpdateCloseValueItem]):
        for it in items:
            await self.add_item(it)

    async def get_items(
        self,
        asset: Asset,
        *,
        start: datetime.datetime | None = None,
        end: datetime.datetime | None = None,
        count: int | None = None,
    ) -> collections.abc.Iterable[UpdateCloseValueItem]:
        items = self._storage.get(asset, [])
        if start:
            items = [i for i in items if i.timestamp >= start.timestamp()]
        if end:
            items = [i for i in items if i.timestamp <= end.timestamp()]
        items = list(items)
        items.sort(key=lambda i: i.timestamp)
        if count is not None:
            items = items[-count:]
        return items


class RedisCandleStorage(CandleStorage):
    """
    Redis-backed candle storage.

    Stores raw price updates in Redis instead of process memory, so they survive
    a restart. Requires the ``redis`` extra (``pip install pocket-option[redis]``).

    Each asset gets two keys:

        - a sorted set of timestamps (for ordering and range queries);
        - a hash mapping the same timestamps to their price value.

    Both are trimmed together after every write so the pair never drifts out of
    sync, and neither grows unbounded - matching the same "keep the most recent
    max_len points" behavior as :class:`MemoryCandleStorage`, just durable.

    Example:

        import redis.asyncio as redis

        storage = RedisCandleStorage(client, redis_client=redis.from_url("redis://localhost:6379/0"))
        ...
        candles = await storage.get_candles(Asset.AUDCAD_otc, timeframe=60)
    """

    def __init__(
        self,
        client: PocketOptionClient,
        *,
        redis_client: typing.Any = None,
        redis_url: str | None = None,
        key_prefix: str = "po:candles",
        max_len: int = 10_000,
    ) -> None:
        """
        :param client: Owning PocketOption client.
        :type client: PocketOptionClient

        :param redis_client: An existing ``redis.asyncio.Redis`` instance to reuse
            (e.g. one already shared elsewhere in the app). Takes priority over
            ``redis_url`` if both are given.
        :type redis_client: redis.asyncio.Redis | None

        :param redis_url: Connection URL, used to build a client if ``redis_client``
            isn't given. Falls back to the ``REDIS_URL`` environment variable, then
            ``redis://localhost:6379/0``.
        :type redis_url: str | None

        :param key_prefix: Redis key namespace, in case multiple things share one
            Redis instance/database.
        :type key_prefix: str

        :param max_len: Maximum number of stored price updates per asset.
        :type max_len: int
        """
        super().__init__(client)
        try:
            import redis.asyncio as redis_asyncio
        except ImportError as exc:
            raise ImportError(
                "RedisCandleStorage requires the 'redis' package - install it with "
                "`pip install pocket-option[redis]` or `pip install redis`.",
            ) from exc

        if redis_client is not None:
            self._redis = redis_client
        else:
            url = redis_url or os.environ.get("REDIS_URL", "redis://localhost:6379/0")
            self._redis = redis_asyncio.from_url(url, decode_responses=True)

        self._key_prefix = key_prefix
        self._max_len = max_len

    def set_max_len(self, max_len: int) -> None:
        """
        Change the maximum number of stored price updates per asset.

        Applies to future writes - existing data beyond the new limit is trimmed
        the next time that asset receives a tick, not immediately (unlike
        :meth:`MemoryCandleStorage.set_max_len`, this can't rewrite everything
        synchronously since Redis access is async).

        :param max_len: Maximum entries per asset.
        :type max_len: int
        """
        self._max_len = max_len

    def _zkey(self, asset: Asset) -> str:
        return f"{self._key_prefix}:{asset.value}:z"

    def _hkey(self, asset: Asset) -> str:
        return f"{self._key_prefix}:{asset.value}:h"

    @staticmethod
    def _member(timestamp: float) -> str:
        # A plain repr of the float is a stable, unique key per timestamp - matches
        # append_or_replace's ["asset", "timestamp"] equality used by
        # MemoryCandleStorage, since HSET/ZADD both naturally overwrite on a
        # repeated member instead of duplicating.
        return repr(timestamp)

    async def get_first_item(self, asset: Asset) -> UpdateCloseValueItem | None:
        members = await self._redis.zrange(self._zkey(asset), 0, 0)
        if not members:
            return None
        value = await self._redis.hget(self._hkey(asset), members[0])
        if value is None:
            return None
        return UpdateCloseValueItem(asset=asset, timestamp=float(members[0]), value=value)

    async def add_item(self, item: UpdateCloseValueItem) -> None:
        await self.add_item_bulk([item])

    async def add_item_bulk(self, items: list[UpdateCloseValueItem]) -> None:
        if not items:
            return
        by_asset: dict[Asset, list[UpdateCloseValueItem]] = defaultdict(list)
        for item in items:
            by_asset[item.asset].append(item)

        for asset, asset_items in by_asset.items():
            zkey, hkey = self._zkey(asset), self._hkey(asset)
            zmapping = {self._member(it.timestamp): it.timestamp for it in asset_items}
            hmapping = {self._member(it.timestamp): str(it.value) for it in asset_items}

            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.zadd(zkey, zmapping)
                pipe.hset(hkey, mapping=hmapping)
                await pipe.execute()

            count = await self._redis.zcard(zkey)
            if count > self._max_len:
                excess = count - self._max_len
                victims = await self._redis.zrange(zkey, 0, excess - 1)
                if victims:
                    async with self._redis.pipeline(transaction=True) as pipe:
                        pipe.zremrangebyrank(zkey, 0, excess - 1)
                        pipe.hdel(hkey, *victims)
                        await pipe.execute()

    async def get_items(
        self,
        asset: Asset,
        *,
        start: datetime.datetime | None = None,
        end: datetime.datetime | None = None,
        count: int | None = None,
    ) -> collections.abc.Iterable[UpdateCloseValueItem]:
        min_score = start.timestamp() if start else "-inf"
        max_score = end.timestamp() if end else "+inf"
        members = await self._redis.zrangebyscore(self._zkey(asset), min_score, max_score)
        if not members:
            return []
        if count is not None:
            members = members[-count:]

        values = await self._redis.hmget(self._hkey(asset), members)
        items = []
        for member, value in zip(members, values, strict=True):
            if value is None:  # trimmed between the zset and hash reads - skip it
                continue
            items.append(UpdateCloseValueItem(asset=asset, timestamp=float(member), value=value))
        return items


class JSONCandleStorage(MemoryCandleStorage):
    TYPE_ADAPTER = pydantic.TypeAdapter(dict[Asset, deque[UpdateCloseValueItem]])

    def __init__(self, client: PocketOptionClient) -> None:
        super().__init__(client)
        self.path = pathlib.Path("reverse", "candles.json")
        if os.environ.get("PO_DEBUG") != "1":
            warnings.warn(
                "JSONCandleStorage is intended for development/testing only. Do not use it in production.",
                RuntimeWarning,
                stacklevel=2,
            )

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(self.TYPE_ADAPTER.dump_json(self._storage, indent=2))

    async def add_item(self, item: UpdateCloseValueItem):
        await super().add_item(item)
        self.save()
