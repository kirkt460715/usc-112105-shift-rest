"""岗位覆盖核算。

与个人工时分开：
- 个人工时按人合并，同一人同时出现在两个点也只计一次；
- 岗位覆盖按点判定，需要该点上有人、且这一刻没有别处同时占用他。

覆盖结论分四类，缺签到不得混同于安全：
- 覆盖：有现场签到或已确认借调在该点；
- 争议：同一人同一时刻跨点，或缺现场带班确认的借调，无法确定哪个点有效；
- 失去覆盖：有肯定证据表明该点无人（当事人被确认借调去别处、或在休息且无替班），
  或必守岗位虽有签到却仍在休息空档；
- 未观察：没有证据，既不能算覆盖，也不能算安全。

等候指令（standby）计入个人义务工时，但不单独构成岗位现场覆盖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import Duty, Materials
from .obligations import PersonTimeline, build_timelines
from .timeutil import (
    MINUTE,
    Interval,
    clip,
    format_instant,
    normalize,
    subtract,
    total,
)

COVERED = "covered"
CONTESTED = "contested"
UNCOVERED = "uncovered"
UNOBSERVED = "unobserved"

SITE_CERTAIN = "certain"
SITE_CONTESTED = "contested"


@dataclass
class SiteClaim:
    person: str
    site: str
    interval: Interval
    certainty: str
    sources: Tuple[str, ...]


@dataclass
class Segment:
    person: str
    interval: Interval
    sites: Dict[str, str]
    note: str = ""

    @property
    def duplicated(self) -> bool:
        return len(self.sites) > 1


@dataclass
class DutyCoverage:
    duty: Duty
    covered: List[Interval] = field(default_factory=list)
    contested: List[Interval] = field(default_factory=list)
    uncovered: List[Interval] = field(default_factory=list)
    unobserved: List[Interval] = field(default_factory=list)
    relief_sources: List[str] = field(default_factory=list)
    absence_notes: List[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.uncovered:
            return UNCOVERED
        if self.contested:
            return CONTESTED
        if self.unobserved:
            return UNOBSERVED
        return COVERED

    @property
    def span_minutes(self) -> int:
        return (self.duty.stop - self.duty.start) // MINUTE

    def minutes(self, pieces: Sequence[Interval]) -> int:
        return total(pieces) // MINUTE


@dataclass
class CoverageReport:
    duties: Dict[str, DutyCoverage]
    segments: List[Segment]
    personal: Dict[str, dict]

    @property
    def uncovered_mandatory(self) -> List[DutyCoverage]:
        return [d for d in self.duties.values() if d.duty.mandatory and d.status == UNCOVERED]

    @property
    def contested_mandatory(self) -> List[DutyCoverage]:
        return [d for d in self.duties.values() if d.duty.mandatory and d.status == CONTESTED]

    @property
    def unobserved_mandatory(self) -> List[DutyCoverage]:
        return [d for d in self.duties.values() if d.duty.mandatory and d.status == UNOBSERVED]


def _atomic(points: Iterable[int]) -> List[Interval]:
    ordered = sorted(set(points))
    return [Interval(a, b) for a, b in zip(ordered, ordered[1:]) if b > a]


def _claims_for(materials: Materials, timeline: PersonTimeline) -> List[SiteClaim]:
    """岗位占用声明；实际自由休息期间不占用岗位。"""
    free = timeline.free_evidence()
    confirmed = [l for l in materials.loans if l.person == timeline.person and l.evidence == "confirmed"]
    conflict = [l for l in materials.loans if l.person == timeline.person and l.evidence == "conflict"]

    claims: List[SiteClaim] = []
    for record in materials.clock:
        if record.person != timeline.person or record.state == "standby":
            continue
        for piece in subtract([record.span], free):
            claims.append(SiteClaim(record.person, record.site, piece, SITE_CERTAIN, (record.record,)))

    for loan in confirmed:
        for piece in subtract([loan.span], free):
            claims.append(SiteClaim(loan.person, loan.new_site, piece, SITE_CERTAIN, (loan.loan,)))

    for loan in conflict:
        for piece in subtract([loan.span], free):
            claims.append(SiteClaim(loan.person, loan.new_site, piece, SITE_CONTESTED, (loan.loan,)))

    # 已确认借调生效时，原岗在该范围内不再有现场覆盖。
    trimmed: List[SiteClaim] = []
    for claim in claims:
        holes: List[Interval] = []
        for loan in confirmed:
            original = materials.duties.get(loan.original_duty)
            if original is None or original.site != claim.site:
                continue
            if loan.loan in claim.sources:
                continue
            holes.append(loan.span)
        for piece in subtract([claim.interval], holes):
            trimmed.append(
                SiteClaim(claim.person, claim.site, piece, claim.certainty, claim.sources)
            )
    return trimmed


def _segments(materials: Materials, timeline: PersonTimeline, claims: Sequence[SiteClaim]) -> List[Segment]:
    points: List[int] = []
    for claim in claims:
        points.extend([claim.interval.start, claim.interval.stop])
    for loan in materials.loans:
        if loan.person == timeline.person:
            points.extend([loan.start, loan.stop])
    if not points:
        return []

    segments: List[Segment] = []
    for piece in _atomic(points):
        active = [c for c in claims if c.interval.start < piece.stop and piece.start < c.interval.stop]
        if not active:
            continue
        sites: Dict[str, str] = {}
        for claim in active:
            if sites.get(claim.site) == SITE_CONTESTED:
                continue
            if claim.certainty == SITE_CONTESTED and claim.site in sites:
                continue
            sites[claim.site] = claim.certainty
        note = ""
        if len(sites) > 1:
            explained = [
                loan
                for loan in materials.loans
                if loan.person == timeline.person
                and loan.evidence == "confirmed"
                and loan.start <= piece.start
                and loan.stop >= piece.stop
                and loan.new_site in sites
            ]
            if explained:
                sites = {explained[0].new_site: SITE_CERTAIN}
                note = f"借调 {explained[0].loan} 已确认，覆盖计入 {explained[0].new_site}"
            else:
                sites = {site: SITE_CONTESTED for site in sites}
                note = "同一人同一时刻在多个点报在岗，缺现场带班确认，各点覆盖均不确定"
        segments.append(Segment(person=timeline.person, interval=piece, sites=sites, note=note))
    return segments


def _personal_hours(materials: Materials, timelines: Dict[str, PersonTimeline]) -> Dict[str, dict]:
    rows: Dict[str, dict] = {}
    for person, timeline in timelines.items():
        raw = 0
        for record in materials.clock:
            if record.person == person:
                raw += record.span.duration
        for loan in materials.loans:
            if loan.person == person:
                raw += loan.span.duration
        counted = total(timeline.work_certain())
        rows[person] = {
            "raw_claim_seconds": raw,
            "counted_seconds": counted,
            "deduplicated_seconds": max(0, raw - counted),
            "contested_seconds": total([p.interval for p in timeline.contested]),
        }
    return rows


def compute(materials: Materials, timelines: Optional[Dict[str, PersonTimeline]] = None) -> CoverageReport:
    timelines = timelines or build_timelines(materials)
    segments: List[Segment] = []
    site_certain: Dict[str, List[Interval]] = {}
    site_contested: Dict[str, List[Interval]] = {}

    for person, timeline in timelines.items():
        for segment in _segments(materials, timeline, _claims_for(materials, timeline)):
            segments.append(segment)
            for site, certainty in segment.sites.items():
                bucket = site_certain if certainty == SITE_CERTAIN else site_contested
                bucket.setdefault(site, []).append(segment.interval)

    for site in set(site_certain) | set(site_contested):
        site_certain[site] = normalize(site_certain.get(site, []))
        site_contested[site] = subtract(normalize(site_contested.get(site, [])), site_certain.get(site, []))

    duties: Dict[str, DutyCoverage] = {}
    for duty in materials.duties.values():
        item = DutyCoverage(duty=duty)
        item.covered = clip(site_certain.get(duty.site, []), duty.start, duty.stop)
        item.contested = clip(site_contested.get(duty.site, []), duty.start, duty.stop)
        remainder = subtract([duty.span], item.covered + item.contested)

        # 有肯定证据表明当事人不在该岗：已确认借调去别处，或实际自由休息且无替班。
        timeline = timelines.get(duty.person)
        affirmative: List[Interval] = []
        if timeline is not None:
            for loan in timeline.loans:
                if loan.evidence == "confirmed":
                    original = materials.duties.get(loan.original_duty)
                    if original is not None and original.site == duty.site:
                        affirmative.append(loan.span)
            affirmative.extend(timeline.free_evidence())
        affirm = normalize(affirmative)
        # 逐点判定：只有与肯定“不在岗”证据相交的部分算失去覆盖，其余是未观察。
        item.uncovered = clip(affirm, duty.start, duty.stop)
        item.uncovered = [piece for piece in (p.intersect(q) for p in remainder for q in item.uncovered) if piece]
        item.uncovered = normalize(item.uncovered)
        item.unobserved = subtract(remainder, item.uncovered)

        for segment in segments:
            if not segment.interval.overlaps(duty.span):
                continue
            if duty.site in segment.sites and segment.person != duty.person and segment.sites[duty.site] == SITE_CERTAIN:
                if any(piece.overlaps(segment.interval) for piece in item.covered):
                    item.relief_sources.append(segment.person)
        duties[duty.duty] = item

    return CoverageReport(duties=duties, segments=segments, personal=_personal_hours(materials, timelines))


def summary(report: CoverageReport) -> dict:
    def rows(pieces: Sequence[Interval]) -> List[dict]:
        return [
            {
                "start": format_instant(p.start, "America/New_York"),
                "stop": format_instant(p.stop, "America/New_York"),
                "minutes": p.duration // MINUTE,
            }
            for p in pieces
        ]

    return {
        "duties": [
            {
                "duty": item.duty.duty,
                "site": item.duty.site,
                "role": item.duty.role,
                "mandatory": item.duty.mandatory,
                "status": item.status,
                "covered_minutes": total(item.covered) // MINUTE,
                "contested": rows(item.contested),
                "uncovered": rows(item.uncovered),
                "unobserved": rows(item.unobserved),
                "relief_sources": sorted(set(item.relief_sources)),
            }
            for item in sorted(report.duties.values(), key=lambda d: (d.duty.start, d.duty.duty))
        ],
        "personal": report.personal,
    }


def render(report: CoverageReport, tz: str = "America/New_York") -> str:
    lines: List[str] = []
    lines.append("岗位覆盖（属地时间）")
    lines.append("=" * 8)
    lines.append(
        f"必守岗位：失去覆盖 {len(report.uncovered_mandatory)} 个，"
        f"覆盖争议 {len(report.contested_mandatory)} 个，"
        f"观察不足 {len(report.unobserved_mandatory)} 个。"
    )
    for item in sorted(report.duties.values(), key=lambda d: (d.duty.start, d.duty.duty)):
        duty = item.duty
        label = "必守" if duty.mandatory else "非必守"
        status = {
            COVERED: "全覆盖",
            CONTESTED: "有争议",
            UNCOVERED: "失去覆盖",
            UNOBSERVED: "观察不足",
        }[item.status]
        lines.append(
            f"  {duty.duty} {duty.site}/{duty.role}（{label}）{status}"
            f" 班次 {format_instant(duty.start, tz)}→{format_instant(duty.stop, tz)}"
        )
        for title, pieces in (
            ("缺口", item.uncovered),
            ("争议", item.contested),
            ("未观察", item.unobserved),
        ):
            for piece in pieces:
                lines.append(
                    f"      {title} {format_instant(piece.start, tz)}→"
                    f"{format_instant(piece.stop, tz)}（{piece.duration // MINUTE} 分钟）"
                )
        if item.relief_sources:
            lines.append(f"      替班来源：{'、'.join(sorted(set(item.relief_sources)))}")

    doubled = [s for s in report.segments if s.duplicated]
    lines.append("")
    lines.append("同一人跨点占用（个人工时只计一次）")
    if not doubled:
        lines.append("  无。")
    seen = set()
    for segment in sorted(doubled, key=lambda s: (s.person, s.interval.start)):
        key = (segment.person, segment.interval.start, segment.interval.stop)
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            f"  {segment.person} {format_instant(segment.interval.start, tz)}→"
            f"{format_instant(segment.interval.stop, tz)} 于 {'、'.join(sorted(segment.sites))}"
            f"；{segment.note}"
        )

    merged = [row for row in report.personal.items() if row[1]["deduplicated_seconds"] > 0]
    lines.append("")
    lines.append("个人工时去重")
    if not merged:
        lines.append("  无重复申报。")
    for person, row in sorted(merged):
        lines.append(
            f"  {person} 原始申报合计 {row['raw_claim_seconds'] // MINUTE} 分钟，"
            f"去重后计入 {row['counted_seconds'] // MINUTE} 分钟，"
            f"合并 {row['deduplicated_seconds'] // MINUTE} 分钟；"
            f"另争议 {row['contested_seconds'] // MINUTE} 分钟"
        )
    return "\n".join(lines)
