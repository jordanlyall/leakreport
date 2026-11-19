"""Shared types for leak modules: Prices (floors, ETH/USD), Params, LeakResult."""
import bisect
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

ETH = 10**18


def day_of(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()


def day_ts(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())


def add_days(day: str, n: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


@dataclass
class Prices:
    floors: dict[str, dict[str, int]]          # slug -> {day: floor wei}, sparse; carried forward
    current_floor: dict[str, int | None]       # slug -> wei (missing key = unknown)
    last_sale_ts: dict[str, int | None]        # slug -> ts; None = fetched, no sale found
    eth_usd: dict[str, float]                  # day -> USD per ETH
    as_of: str
    _days: dict = field(default_factory=dict, repr=False)

    def _series(self, slug):
        if slug not in self._days:
            s = self.floors.get(slug, {})
            self._days[slug] = sorted(s)
        return self._days[slug]

    def floor(self, slug: str, day: str) -> int | None:
        days = self._series(slug)
        i = bisect.bisect_right(days, day)
        return self.floors[slug][days[i - 1]] if i else None

    def change_days(self, slug: str, start: str, end: str) -> list[str]:
        """Days in (start, end] where the floor series has a point."""
        days = self._series(slug)
        return days[bisect.bisect_right(days, start):bisect.bisect_right(days, end)]

    def median30(self, slug: str, day: str) -> int | None:
        vals = [v for d in (add_days(day, -k) for k in range(30, 0, -1))
                if (v := self.floor(slug, d)) is not None]
        return int(statistics.median_low(vals)) if vals else None

    def usd_rate(self, day: str) -> float | None:
        if not self.eth_usd:
            return None
        days = self._series_usd()
        i = bisect.bisect_right(days, day)
        return self.eth_usd[days[i - 1]] if i else self.eth_usd[days[0]]

    def _series_usd(self):
        if "__usd__" not in self._days:
            self._days["__usd__"] = sorted(self.eth_usd)
        return self._days["__usd__"]


@dataclass
class ChurnTx:
    tx_hash: str
    ts: int
    to: str
    gas_wei: int
    reason: str          # "approval" | "cancel" | "failed" | "weth" | "other"


@dataclass
class Params:
    since: str = "2022-06-14"
    as_of: str = "2026-11-09"
    benchmark_rate: str = "0.005"          # string so the math stays exact (Fraction)
    blur_launch: str = "2022-10-19"
    breach_ratio: str = "0.9"
    dead_days: int = 90
    churn_txs: list[ChurnTx] = field(default_factory=list)

    @property
    def since_ts(self) -> int:
        return day_ts(self.since)

    @property
    def as_of_ts(self) -> int:
        return day_ts(self.as_of) + 86399


@dataclass
class LeakResult:
    id: str
    weight: str                     # "1.0" or "0.5"
    wei: int = 0
    items: list[dict] = field(default_factory=list)   # each has "wei" and "day"
    detail: dict = field(default_factory=dict)


def count_each_lot_once(results: list[LeakResult]) -> list[LeakResult]:
    """A lot that is both held past the floor and dead inventory is one mistake: both weigh 0.5, so it stays only
    in the leak where it is larger (dead inventory on a tie). The other leak notes how many it handed over."""
    by_id = {r.id: r for r in results}
    held, dead = by_id.get("held_past_floor"), by_id.get("dead_inventory")
    if not held or not dead:
        return results
    h = {i["lot_id"]: i["wei"] for i in held.items}
    d = {i["lot_id"]: i["wei"] for i in dead.items}
    to_dead = {k for k in h.keys() & d.keys() if d[k] >= h[k]}
    to_held = (h.keys() & d.keys()) - to_dead

    def trim(r: LeakResult, drop: set, note: str) -> LeakResult:
        items = [i for i in r.items if i["lot_id"] not in drop]
        return LeakResult(r.id, r.weight, sum(i["wei"] for i in items), items, {**r.detail, note: len(drop)})

    new = {"held_past_floor": trim(held, to_dead, "counted_in_dead_inventory"),
           "dead_inventory": trim(dead, to_held, "counted_in_held_past_floor")}
    return [new.get(r.id, r) for r in results]
