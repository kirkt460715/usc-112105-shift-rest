"""按实际在岗义务重建个人时间线。

证据分层：
- 签定班表（duties）是义务，不是“人一定在场”的证明；
- 签到（clock）说明实际在岗或在等候指令；
- 借调（loans）里 confirmed 才把人的位置确定到新岗，conflict 只确定“人在工作，
  但哪个岗位有覆盖说不清”；
- 休息（breaks）里只有 free 与 recall 召回前的部分算可自由支配，standby 不算。

个人工时按人合并，两个点同时报同一人在岗也只计一次。休息判定分三档：
下界只用有证据的自由休息，上界把无记录时间按可能休息计，另有一个“按签定义务
推定”的口径用于比较分组，避免签到缺失让某组显得更安全。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Tuple

from .models import BreakRecord, ClockRecord, Loan, Materials
from .timeutil import (
    Interval,
    clip,
    local_day,
    longest,
    normalize,
    subtract,
    total,
)

CERTAIN = "certain"
CONTESTED = "contested"


@dataclass(frozen=True)
class Piece:
    """一段带来源与确定性的个人时间。"""

    interval: Interval
    kind: str
    certainty: str
    sources: Tuple[str, ...]

    @property
    def start(self) -> int:
        return self.interval.start

    @property
    def stop(self) -> int:
        return self.interval.stop


@dataclass
class PersonTimeline:
    person: str
    tz: str
    onsite: List[Piece] = field(default_factory=list)
    standby: List[Piece] = field(default_factory=list)
    confirmed_loans: List[Piece] = field(default_factory=list)
    contested: List[Piece] = field(default_factory=list)
    free: List[Piece] = field(default_factory=list)
    duties: List[Interval] = field(default_factory=list)
    loans: List[Loan] = field(default_factory=list)
    breaks: List[BreakRecord] = field(default_factory=list)
    clock: List[ClockRecord] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    # -- 个人工时 -----------------------------------------------------

    def _work_pieces(self, include_contested: bool) -> List[Piece]:
        pieces = self.onsite + self.standby + self.confirmed_loans
        if include_contested:
            pieces += self.contested
        return pieces

    def work_certain(self) -> List[Interval]:
        """确定计入个人工时的时段；两个点同时报在岗会并成一段。"""
        return normalize([p.interval for p in self._work_pieces(False)])

    def work_possible(self) -> List[Interval]:
        return normalize([p.interval for p in self._work_pieces(True)])

    def work_obligated(self) -> List[Interval]:
        """签定班表与已确认借调构成的义务；用于“按义务推定”口径。"""
        return normalize([p.interval for p in self.confirmed_loans] + self.duties + [p.interval for p in self.contested])

    def onsite_intervals(self) -> List[Interval]:
        return normalize([p.interval for p in self.onsite])

    def free_evidence(self) -> List[Interval]:
        return normalize([p.interval for p in self.free])

    def rest_lower(self, window: Interval) -> List[Interval]:
        """下界：有证据的自由休息。

        扣掉与之冲突的借调（已确认的说明人确实在别处；冲突的无法确认），
        但签到覆盖整段班次不算冲突——班内休息本来就从签到时段里挖出来。
        """
        borrowed = [p.interval for p in self.confirmed_loans + self.contested]
        allowed = subtract(self.free_evidence(), borrowed)
        return clip(allowed, window.start, window.stop)

    def rest_upper(self, window: Interval) -> List[Interval]:
        """上界：无记录时间按可能休息计；已记录的等候与借调不算休息。

        班次内的正常休息记录会从工作证据里挖掉，因此不会因为签到覆盖整段班次
        而把批准过的休息又算成工作。
        """
        work = subtract(self.work_possible(), self.free_evidence())
        return clip(subtract([Interval(window.start, window.stop)], work), window.start, window.stop)

    def rest_presumed(self, window: Interval) -> List[Interval]:
        """按签定义务推定的休息：无签到也按义务在岗，缺失不能带来额外休息。"""
        work = subtract(self.work_obligated(), self.free_evidence())
        return clip(subtract([Interval(window.start, window.stop)], work), window.start, window.stop)

    def longest_rest(self, window: Interval) -> Tuple[int, int, int]:
        return (
            longest(self.rest_lower(window)),
            longest(self.rest_upper(window)),
            longest(self.rest_presumed(window)),
        )

    def work_total(self, window: Interval) -> int:
        return total(clip(self.work_certain(), window.start, window.stop))

    def continuous_work(self) -> List[Interval]:
        """连续工作窗口：已批准的自由休息会打断连续工作。"""
        return normalize(subtract(self.work_certain(), self.free_evidence()))

    def longest_work_stretch(self) -> Interval:
        pieces = self.continuous_work()
        if not pieces:
            return Interval(0, 0)
        return max(pieces, key=lambda item: item.duration)

    def local_work_days(self) -> List[Tuple[str, int, int, int]]:
        """属地工作日、当日真实长度、确定工时与按义务推定义务工时。

        回拨日按 25 小时计，用工时占比而不是绝对小时解释跨午夜班次。
        """
        certain = self.work_certain()
        presumed = self.work_obligated()
        days = sorted({local_day(interval.start, self.tz) for interval in certain + presumed})
        rows: List[Tuple[str, int, int, int]] = []
        for day in days:
            day_start = _day_start(day, self.tz)
            day_stop = _day_start(_next_day(day), self.tz)
            rows.append(
                (
                    day,
                    day_stop - day_start,
                    total(clip(certain, day_start, day_stop)),
                    total(clip(presumed, day_start, day_stop)),
                )
            )
        return rows

    def gaps_in_duties(self) -> List[Interval]:
        """签定班表内没有签到或借调证据的时段。"""
        observed = normalize([p.interval for p in self.onsite + self.standby + self.confirmed_loans + self.contested])
        holes: List[Interval] = []
        for duty in self.duties:
            holes.extend(subtract([duty], observed))
        return normalize(holes)


def _day_start(day: str, tz: str) -> int:
    from datetime import date, datetime, timedelta
    from zoneinfo import ZoneInfo

    moment = datetime.fromisoformat(day)
    # 回拨日的凌晨 00:00 不歧义；用当日零点起算才能得到 25 小时的工作日。
    return int(moment.replace(tzinfo=ZoneInfo(tz)).timestamp())


def _next_day(day: str) -> str:
    from datetime import date, timedelta

    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def _merge_pieces(pieces: Iterable[Piece]) -> List[Piece]:
    ordered = sorted(pieces, key=lambda p: (p.start, p.stop))
    merged: List[Piece] = []
    for piece in ordered:
        if merged and piece.start <= merged[-1].stop:
            last = merged[-1]
            merged[-1] = Piece(
                Interval(last.start, max(last.stop, piece.stop)),
                last.kind,
                last.certainty,
                tuple(sorted(set(last.sources) | set(piece.sources))),
            )
        else:
            merged.append(piece)
    return merged


def build_timelines(materials: Materials) -> Dict[str, PersonTimeline]:
    timelines: Dict[str, PersonTimeline] = {}

    def timeline(person: str) -> PersonTimeline:
        if person not in timelines:
            timelines[person] = PersonTimeline(person=person, tz=materials.tz_for(person))
        return timelines[person]

    for duty in materials.duties.values():
        timeline(duty.person).duties.append(duty.span)

    for record in materials.clock:
        line = timeline(record.person)
        line.clock.append(record)
        if record.state == "onsite":
            line.onsite.append(Piece(record.span, "onsite", CERTAIN, (record.record,)))
        elif record.state == "standby":
            line.standby.append(Piece(record.span, "standby", CERTAIN, (record.record,)))

    for loan in materials.loans:
        line = timeline(loan.person)
        line.loans.append(loan)
        if loan.evidence == "confirmed":
            line.confirmed_loans.append(
                Piece(loan.span, "confirmed_loan", CERTAIN, (loan.loan,))
            )
        else:
            line.contested.append(Piece(loan.span, "conflict_loan", CONTESTED, (loan.loan,)))
            line.notes.append(
                f"{loan.loan}：借调证据冲突，原岗 {loan.original_duty} 与新岗 {loan.new_site} "
                "在重叠时段的覆盖都无法确认；个人工时按可能值计入上界。"
            )

    for record in materials.breaks:
        line = timeline(record.person)
        line.breaks.append(record)
        if record.evidence == "standby":
            line.standby.append(Piece(record.span, "standby_break", CERTAIN, (record.break_id,)))
            continue
        if record.evidence == "recall":
            if record.recall_at is None:
                line.notes.append(
                    f"{record.break_id}：标为 recall 但没有召回时间，按整段计自由休息，证据待核。"
                )
            elif not (record.start <= record.recall_at <= record.stop):
                line.notes.append(
                    f"{record.break_id}：召回时间落在记录区间之外，已按裁剪后的有效段计算。"
                )
        effective = record.effective_free()
        if effective is not None:
            line.free.append(Piece(effective, "free", CERTAIN, (record.break_id,)))

    for line in timelines.values():
        line.onsite = _merge_pieces(line.onsite)
        line.standby = _merge_pieces(line.standby)
        line.confirmed_loans = _merge_pieces(line.confirmed_loans)
        line.contested = _merge_pieces(line.contested)
        line.free = _merge_pieces(line.free)
        line.duties = normalize(line.duties)
    return timelines
