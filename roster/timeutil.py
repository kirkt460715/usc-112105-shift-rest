"""时间与区间的基础运算。

所有时间先折算成 UTC 瞬时（POSIX 秒）再比较。带偏移量的记录按偏移量记录的
真实经过时间处理；不带偏移量的记录按属地时区解释。区间一律包含起点、不含终点。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

SECOND = 1
MINUTE = 60
HOUR = 3600


class TimeError(ValueError):
    """时间无法解释。"""


def parse_instant(value: str, default_tz: str) -> int:
    """把记录中的时间折成 UTC 秒。

    带偏移量（+08:00 / Z）的时间照其偏移量换算，因此秋季回拨的
    01:10-04:00 与 01:45-05:00 之间是 35 分钟实际经过时间，而非 25 分钟，
    也不是按墙上时间计的 95 分钟。
    """
    if not isinstance(value, str) or not value:
        raise TimeError("时间为空")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as error:
        raise TimeError(f"无法解析时间：{value!r}") from error
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo(default_tz))
    return int(moment.timestamp())


def parse_optional(value: Optional[str], default_tz: str) -> Optional[int]:
    if value is None:
        return None
    return parse_instant(value, default_tz)


def format_instant(epoch: int, tz: str) -> str:
    """按属地带偏移量呈现，便于人工核对回拨时段。"""
    moment = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(ZoneInfo(tz))
    return moment.isoformat(timespec="seconds")


def utc_text(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def local_day(epoch: int, tz: str) -> str:
    """属地工作日（日历日），按该点位的墙上日期。"""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(ZoneInfo(tz)).date().isoformat()


def local_date_range(start: int, stop: int, tz: str) -> List[str]:
    """区间覆盖到的属地日历日；区间为半开，故终止日按 stop-1 秒计。"""
    if stop <= start:
        return []
    days: List[str] = []
    day = datetime.fromtimestamp(start, tz=timezone.utc).astimezone(ZoneInfo(tz)).date()
    last = datetime.fromtimestamp(stop - SECOND, tz=timezone.utc).astimezone(ZoneInfo(tz)).date()
    while day <= last:
        days.append(day.isoformat())
        day += timedelta(days=1)
    return days


@dataclass(frozen=True, order=True)
class Interval:
    """半开区间 [start, stop)，秒为单位的 UTC 瞬时。"""

    start: int
    stop: int

    def __post_init__(self) -> None:
        if self.stop < self.start:
            raise TimeError(f"区间终点早于起点：{self.start} -> {self.stop}")

    @property
    def duration(self) -> int:
        return self.stop - self.start

    @property
    def is_empty(self) -> bool:
        return self.stop <= self.start

    def overlaps(self, other: "Interval") -> bool:
        return self.start < other.stop and other.start < self.stop

    def intersect(self, other: "Interval") -> Optional["Interval"]:
        start = max(self.start, other.start)
        stop = min(self.stop, other.stop)
        return Interval(start, stop) if stop > start else None

    def contains(self, epoch: int) -> bool:
        return self.start <= epoch < self.stop

    def clamp(self, low: int, high: int) -> Optional["Interval"]:
        return self.intersect(Interval(low, high))


def normalize(intervals: Iterable[Interval]) -> List[Interval]:
    """合并重叠或首尾相接的区间。"""
    ordered = sorted((item for item in intervals if not item.is_empty))
    merged: List[Interval] = []
    for item in ordered:
        if merged and item.start <= merged[-1].stop:
            merged[-1] = Interval(merged[-1].start, max(merged[-1].stop, item.stop))
        else:
            merged.append(item)
    return merged


def intersect_all(groups: Sequence[Sequence[Interval]]) -> List[Interval]:
    """多个区间集合的共同覆盖。"""
    if not groups:
        return []
    current = normalize(groups[0])
    for group in groups[1:]:
        other = normalize(group)
        product: List[Interval] = []
        for left in current:
            for right in other:
                overlap = left.intersect(right)
                if overlap is not None:
                    product.append(overlap)
        current = normalize(product)
        if not current:
            break
    return current


def subtract(base: Sequence[Interval], holes: Sequence[Interval]) -> List[Interval]:
    """base 减去 holes。"""
    result: List[Interval] = []
    holes_merged = normalize(holes)
    for item in normalize(base):
        cursor = item.start
        for hole in holes_merged:
            if hole.stop <= cursor or hole.start >= item.stop:
                continue
            if hole.start > cursor:
                result.append(Interval(cursor, min(hole.start, item.stop)))
            cursor = max(cursor, hole.stop)
            if cursor >= item.stop:
                break
        if cursor < item.stop:
            result.append(Interval(cursor, item.stop))
    return normalize(result)


def merge_within(intervals: Iterable[Interval], gap: int = 0) -> List[Interval]:
    """把间隔不超过 gap 秒的区间视为连续（用于休息的可间断容忍）。"""
    ordered = sorted((item for item in intervals if not item.is_empty), key=lambda i: (i.start, i.stop))
    merged: List[Interval] = []
    for item in ordered:
        if merged and item.start - merged[-1].stop <= gap:
            merged[-1] = Interval(merged[-1].start, max(merged[-1].stop, item.stop))
        else:
            merged.append(item)
    return merged


def longest(intervals: Sequence[Interval]) -> int:
    return max((item.duration for item in intervals), default=0)


def total(intervals: Sequence[Interval]) -> int:
    return sum(item.duration for item in normalize(intervals))


def clip(intervals: Sequence[Interval], low: int, high: int) -> List[Interval]:
    window = Interval(low, high)
    return [piece for piece in (item.intersect(window) for item in normalize(intervals)) if piece]


def hhmm(seconds: int) -> str:
    """把秒数呈现成 h:mm，用于报告。"""
    sign = "-" if seconds < 0 else ""
    seconds = abs(int(seconds))
    hours, rest = divmod(seconds, HOUR)
    minutes, secs = divmod(rest, MINUTE)
    if secs:
        return f"{sign}{hours}:{minutes:02d}:{secs:02d}"
    return f"{sign}{hours}:{minutes:02d}"


def span_text(interval: Interval, tz: str) -> str:
    return f"{format_instant(interval.start, tz)} → {format_instant(interval.stop, tz)}"


def adjacent(a: Interval, b: Interval, tolerance: int = 0) -> bool:
    return abs(a.stop - b.start) <= tolerance or abs(b.stop - a.start) <= tolerance


def covered_seconds(intervals: Sequence[Interval], low: int, high: int) -> int:
    return total(clip(intervals, low, high))


def union_endpoints(intervals: Iterable[Interval]) -> Tuple[int, int]:
    items = list(intervals)
    if not items:
        return (0, 0)
    return (min(i.start for i in items), max(i.stop for i in items))
