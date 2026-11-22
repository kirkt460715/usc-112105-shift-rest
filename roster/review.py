"""月度复核：连续休息、观察覆盖、人群比较与补休欠账。

判定口径：
- 观察窗从每次签定班表的起点起算 window_hours。协议要求窗内有一段
  required_free_minutes 的连续自由休息。
- 三档口径同时保留：
  * 下界：只用有证据的自由休息；
  * 上界：无记录时间按可能休息计；
  * 推定：按签定义务在岗计算，签到缺失不会凭空多出休息。
- 三档结论：
  * 已确认欠休：上界也不足；
  * 还可能欠休：上界够、下界不够，且观察覆盖达标；
  * 观察不足：上界够、下界不够，但窗内义务的观察覆盖低于协议下限。
    这类不能判为“已满足”，也不能直接生成补休。
- 同一人同 dedup_group 且相交的不足窗合并为一张工单，补休取触发条款最高值。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import Agreement, Duty, Materials, RestPromise
from .obligations import PersonTimeline, build_timelines
from .timeutil import (
    MINUTE,
    Interval,
    clip,
    format_instant,
    hhmm,
    normalize,
    subtract,
    total,
)

MET = "met"
CONFIRMED = "confirmed"
POSSIBLE = "possible"
UNDERDETERMINED = "underdetermined"

STATUS_TEXT = {
    MET: "已满足",
    CONFIRMED: "已确认欠休",
    POSSIBLE: "还可能欠休",
    UNDERDETERMINED: "观察不足",
}


@dataclass
class Dispute:
    interval: Interval
    reason: str
    sources: Tuple[str, ...]


@dataclass
class RestWindow:
    person: str
    group: str
    duty_id: str
    rule: Agreement
    window: Interval
    required: int
    rest_lower: int
    rest_upper: int
    rest_presumed: int
    obligation: int
    observed: int
    coverage: float
    status: str
    layer: str = "standard"
    disputes: List[Dispute] = field(default_factory=list)

    @property
    def shortfall_lower(self) -> int:
        return max(0, self.required - self.rest_lower)

    @property
    def shortfall_upper(self) -> int:
        return max(0, self.required - self.rest_upper)


@dataclass
class Finding:
    person: str
    group: str
    dedup_group: str
    rule_ids: List[str]
    duties: List[str]
    windows: List[Interval]
    status: str
    required: int
    rest_lower: int
    rest_upper: int
    coverage: float
    compensation_minutes: int
    disputes: List[Dispute] = field(default_factory=list)
    finding_id: str = ""
    promise: Optional[str] = None
    scheduled_minutes: int = 0

    @property
    def scope(self) -> Interval:
        return Interval(min(w.start for w in self.windows), max(w.stop for w in self.windows))

    @property
    def outstanding_minutes(self) -> int:
        if self.status != CONFIRMED:
            return 0
        return max(0, self.compensation_minutes - self.scheduled_minutes)


@dataclass
class GroupStats:
    group: str
    windows: int = 0
    evaluable: int = 0
    met: int = 0
    confirmed: int = 0
    possible: int = 0
    underdetermined: int = 0
    obligation_seconds: int = 0
    observed_seconds: int = 0
    night_windows: int = 0
    night_obligation_seconds: int = 0
    night_observed_seconds: int = 0
    confirmed_shortfall_seconds: int = 0
    rest_lower_seconds: int = 0
    rest_upper_seconds: int = 0
    rest_presumed_seconds: int = 0
    required_seconds: int = 0

    @property
    def coverage(self) -> float:
        return self.observed_seconds / self.obligation_seconds if self.obligation_seconds else 0.0

    @property
    def night_coverage(self) -> float:
        return (
            self.night_observed_seconds / self.night_obligation_seconds
            if self.night_obligation_seconds
            else 0.0
        )

    @property
    def confirmed_rate(self) -> float:
        return self.confirmed / self.evaluable if self.evaluable else 0.0

    @property
    def average_rest(self) -> int:
        return self.rest_lower_seconds // self.windows if self.windows else 0

    @property
    def average_rest_upper(self) -> int:
        return self.rest_upper_seconds // self.windows if self.windows else 0

    @property
    def average_rest_presumed(self) -> int:
        return self.rest_presumed_seconds // self.windows if self.windows else 0


@dataclass
class Arrears:
    finding_id: str
    person: str
    group: str
    status: str
    scope: Interval
    owed_minutes: int
    scheduled_minutes: int
    promise: Optional[str]
    state: str


@dataclass
class Review:
    windows: List[RestWindow]
    findings: List[Finding]
    groups: Dict[str, GroupStats]
    arrears: List[Arrears]
    clock_gaps: List[dict]
    person_notes: Dict[str, List[str]]
    timelines: Dict[str, PersonTimeline] = field(default_factory=dict)

    @property
    def confirmed(self) -> List[Finding]:
        return [f for f in self.findings if f.status == CONFIRMED]

    @property
    def possible(self) -> List[Finding]:
        return [f for f in self.findings if f.status == POSSIBLE]

    @property
    def underdetermined(self) -> List[Finding]:
        return [f for f in self.findings if f.status == UNDERDETERMINED]


def _duty_index(materials: Materials) -> Dict[str, List[Duty]]:
    index: Dict[str, List[Duty]] = {}
    for duty in materials.duties.values():
        index.setdefault(duty.person, []).append(duty)
    for duties in index.values():
        duties.sort(key=lambda d: (d.start, d.stop))
    return index


def _window_for(duty: Duty, rule: Agreement) -> Interval:
    return Interval(duty.start, duty.start + rule.window_seconds)


def _coverage_in_window(materials: Materials, person: str, window: Interval) -> Tuple[int, int]:
    """窗内签定班表的义务时间，以及其中有签到或借调证据的时间。"""
    duties = [d.span for d in materials.duties.values() if d.person == person]
    obligation = total(clip(duties, window.start, window.stop))
    observed_parts: List[Interval] = []
    for record in materials.clock:
        if record.person == person:
            observed_parts.append(record.span)
    for loan in materials.loans:
        if loan.person == person:
            observed_parts.append(loan.span)
    observed = total([p for p in (piece.intersect(window) for piece in normalize(observed_parts)) if p])
    return obligation, min(observed, obligation)


def _disputes(timeline: PersonTimeline, window: Interval) -> List[Dispute]:
    """窗内与结论有关的证据冲突。"""
    disputes: List[Dispute] = []
    for piece in timeline.contested:
        overlap = piece.interval.intersect(window)
        if overlap is not None:
            disputes.append(
                Dispute(
                    interval=overlap,
                    reason="借调证据冲突：原岗与新岗在此时段哪边有覆盖无法确认",
                    sources=piece.sources,
                )
            )
    # 被召回截断的休息单独说明：后续事件改变了原批准判断。
    for record in timeline.breaks:
        if record.evidence != "recall" or record.recall_at is None:
            continue
        overlap = record.span.intersect(window)
        if overlap is None:
            continue
        effective = record.effective_free()
        lost_start = effective.stop if effective else record.start
        disputes.append(
            Dispute(
                interval=Interval(max(lost_start, window.start), min(record.stop, window.stop)),
                reason=(
                    f"休息 {record.break_id} 原批准至 {format_instant(record.stop, timeline.tz)}，"
                    f"于 {format_instant(record.recall_at, timeline.tz)} 被召回而终止"
                ),
                sources=(record.break_id,),
            )
        )
    return disputes


def _evaluate_window(
    materials: Materials,
    timeline: PersonTimeline,
    duty: Duty,
    rule: Agreement,
) -> RestWindow:
    window = _window_for(duty, rule)
    obligation, observed = _coverage_in_window(materials, timeline.person, window)
    coverage = observed / obligation if obligation else 1.0

    rest_lower, rest_upper, rest_presumed = timeline.longest_rest(window)
    required = rule.required_free_seconds

    if rest_upper < required:
        status = CONFIRMED
    elif rest_lower >= required:
        status = MET
    elif coverage < rule.minimum_coverage:
        status = UNDERDETERMINED
    else:
        status = POSSIBLE

    return RestWindow(
        person=timeline.person,
        group=materials.group_of(timeline.person),
        duty_id=duty.duty,
        rule=rule,
        window=window,
        required=required,
        rest_lower=rest_lower,
        rest_upper=rest_upper,
        rest_presumed=rest_presumed,
        obligation=obligation,
        observed=observed,
        coverage=coverage,
        status=status,
        layer=rule.scope,
        disputes=_disputes(timeline, window),
    )


def _dedupe(windows: Sequence[RestWindow]) -> List[Finding]:
    flagged = [w for w in windows if w.status != MET]
    buckets: Dict[Tuple[str, str], List[RestWindow]] = {}
    for item in flagged:
        buckets.setdefault((item.person, item.rule.dedup_group), []).append(item)

    findings: List[Finding] = []
    for (person, dedup_group), items in buckets.items():
        items.sort(key=lambda w: (w.window.start, w.window.stop, w.rule.rule))
        clusters: List[List[RestWindow]] = []
        for item in items:
            if clusters and item.window.start < max(w.window.stop for w in clusters[-1]):
                clusters[-1].append(item)
            else:
                clusters.append([item])
        for cluster in clusters:
            statuses = {w.status for w in cluster}
            if CONFIRMED in statuses:
                status = CONFIRMED
            elif POSSIBLE in statuses:
                status = POSSIBLE
            else:
                status = UNDERDETERMINED
            compensation = 0
            if status == CONFIRMED:
                # 同一时段触发多条不足条款，只取最高补休，避免重复承诺。
                compensation = max(
                    w.rule.compensation_minutes for w in cluster if w.status == CONFIRMED
                )
            disputes: List[Dispute] = []
            seen = set()
            for w in cluster:
                for dispute in w.disputes:
                    if dispute.interval.is_empty:
                        continue
                    key = (dispute.interval.start, dispute.interval.stop, dispute.reason)
                    if key not in seen:
                        seen.add(key)
                        disputes.append(dispute)
            findings.append(
                Finding(
                    person=person,
                    group=cluster[0].group,
                    dedup_group=dedup_group,
                    rule_ids=sorted({w.rule.rule for w in cluster}),
                    duties=sorted({w.duty_id for w in cluster}),
                    windows=[w.window for w in cluster],
                    status=status,
                    required=max(w.required for w in cluster),
                    rest_lower=min(w.rest_lower for w in cluster),
                    rest_upper=min(w.rest_upper for w in cluster),
                    coverage=min(w.coverage for w in cluster),
                    compensation_minutes=compensation,
                    disputes=disputes,
                )
            )

    findings.sort(key=lambda f: (f.scope.start, f.person, f.dedup_group))
    for number, finding in enumerate(findings, 1):
        finding.finding_id = f"F{number:03d}"
    return findings


def _clock_gaps(materials: Materials, timelines: Dict[str, PersonTimeline]) -> List[dict]:
    gaps: List[dict] = []
    for person in sorted(timelines):
        timeline = timelines[person]
        for hole in timeline.gaps_in_duties():
            if hole.duration >= MINUTE:
                duty = next(
                    (d for d in materials.duties.values() if d.person == person and d.span.overlaps(hole)),
                    None,
                )
                gaps.append(
                    {
                        "person": person,
                        "duty": duty.duty if duty else None,
                        "site": duty.site if duty else None,
                        "interval": hole,
                        "minutes": hole.duration // MINUTE,
                    }
                )
    gaps.sort(key=lambda g: (g["interval"].start, g["person"]))
    return gaps


def _group_stats(materials: Materials, windows: Sequence[RestWindow]) -> Dict[str, GroupStats]:
    stats: Dict[str, GroupStats] = {}
    # 夜间条款的观察窗会跨相邻班次；按人合并相交窗，避免同一段夜间义务被计两次。
    night_windows: Dict[str, List[Interval]] = {}
    for item in windows:
        if item.rule.scope == "night":
            night_windows.setdefault(item.person, []).append((item, item.window))
    seen_night: Dict[str, List[Interval]] = {}
    for item in windows:
        group = stats.setdefault(item.group, GroupStats(group=item.group))
        group.windows += 1
        group.obligation_seconds += item.obligation
        group.observed_seconds += item.observed
        group.rest_lower_seconds += item.rest_lower
        group.rest_upper_seconds += item.rest_upper
        group.rest_presumed_seconds += item.rest_presumed
        group.required_seconds += item.required
        if item.obligation:
            group.evaluable += 1
        if item.status == MET:
            group.met += 1
        elif item.status == CONFIRMED:
            group.confirmed += 1
            group.confirmed_shortfall_seconds += item.shortfall_lower
        elif item.status == POSSIBLE:
            group.possible += 1
        else:
            group.underdetermined += 1
        if item.rule.scope != "night":
            continue
        # 该窗已被同人更早的夜间窗覆盖的部分不再重复计入。
        already = seen_night.setdefault(item.person, [])
        novel = subtract([item.window], already)
        if not novel:
            continue
        seen_night[item.person] = normalize(already + [item.window])
        group.night_windows += 1
        overlap_seconds = total(novel)
        group.night_obligation_seconds += overlap_seconds
        group.night_observed_seconds += overlap_seconds * item.coverage
    return stats


def _attach_promises(materials: Materials, findings: Sequence[Finding]) -> None:
    for finding in findings:
        if finding.status != CONFIRMED:
            continue
        candidates: List[RestPromise] = []
        for promise in materials.rest_promises.values():
            if promise.person != finding.person:
                continue
            if promise.source_duty:
                if promise.source_duty in finding.duties:
                    candidates.append(promise)
            elif any(promise.span.overlaps(w) for w in finding.windows):
                candidates.append(promise)
        if candidates:
            best = max(candidates, key=lambda p: p.span.duration)
            finding.promise = best.promise
            finding.scheduled_minutes = min(best.span.duration // MINUTE, finding.compensation_minutes)


def _arrears(findings: Sequence[Finding]) -> List[Arrears]:
    items: List[Arrears] = []
    for finding in findings:
        if finding.status == CONFIRMED:
            state = "已签收待兑现" if finding.promise else "未安排"
            items.append(
                Arrears(
                    finding_id=finding.finding_id,
                    person=finding.person,
                    group=finding.group,
                    status=finding.status,
                    scope=finding.scope,
                    owed_minutes=max(0, finding.compensation_minutes - finding.scheduled_minutes),
                    scheduled_minutes=finding.scheduled_minutes,
                    promise=finding.promise,
                    state=state,
                )
            )
        else:
            items.append(
                Arrears(
                    finding_id=finding.finding_id,
                    person=finding.person,
                    group=finding.group,
                    status=finding.status,
                    scope=finding.scope,
                    owed_minutes=0,
                    scheduled_minutes=0,
                    promise=None,
                    state="待调查",
                )
            )
    return items


def review(materials: Materials) -> Review:
    timelines = build_timelines(materials)
    duty_index = _duty_index(materials)
    windows: List[RestWindow] = []

    for person in materials.persons():
        timeline = timelines.get(person)
        if timeline is None:
            continue
        rules = materials.rules_for(person)
        for duty in duty_index.get(person, []):
            for rule in rules:
                windows.append(_evaluate_window(materials, timeline, duty, rule))

    findings = _dedupe(windows)
    _attach_promises(materials, findings)
    groups = _group_stats(materials, windows)
    gaps = _clock_gaps(materials, timelines)
    notes = {p: list(t.notes) for p, t in timelines.items() if t.notes}
    return Review(
        windows=windows,
        findings=findings,
        groups=groups,
        arrears=_arrears(findings),
        clock_gaps=gaps,
        person_notes=notes,
        timelines=timelines,
    )


# -- 呈现 -----------------------------------------------------------------


def _scope_text(finding: Finding, tz: str) -> str:
    return " / ".join(
        f"{format_instant(w.start, tz)}→{format_instant(w.stop, tz)}" for w in finding.windows
    )


def render_review(materials: Materials, result: Review, tz: str = "America/New_York") -> str:
    lines: List[str] = []
    lines.append("月度复核")
    lines.append("=" * 8)
    confirmed, possible, under = result.confirmed, result.possible, result.underdetermined
    lines.append(
        f"观察窗 {len(result.windows)} 个；已确认欠休 {len(confirmed)} 件，"
        f"还可能欠休 {len(possible)} 件，观察不足未判定 {len(under)} 件。"
    )

    lines.append("")
    lines.append("一、已确认欠休（可生成工单）")
    if not confirmed:
        lines.append("  无。")
    for finding in confirmed:
        lines.append(
            f"  {finding.finding_id} {finding.person}（{finding.group}）"
            f" 条款 {'+'.join(finding.rule_ids)}"
            f" 要求连续自由休息 {finding.required // MINUTE} 分钟，"
            f"实测上界 {finding.rest_upper // MINUTE} 分钟"
        )
        lines.append(f"      缺口范围：{_scope_text(finding, tz)}")
        lines.append(
            f"      合并组 {finding.dedup_group}，补休 {finding.compensation_minutes} 分钟；"
            f"观察覆盖 {finding.coverage:.0%}"
        )
        if finding.promise:
            lines.append(
                f"      已签收补休 {finding.promise}，兑现 {finding.scheduled_minutes} 分钟，"
                f"尚欠 {finding.outstanding_minutes} 分钟"
            )
        for dispute in finding.disputes:
            lines.append(
                f"      证据：{format_instant(dispute.interval.start, tz)}→"
                f"{format_instant(dispute.interval.stop, tz)} {dispute.reason}"
                f"（{'、'.join(dispute.sources)}）"
            )

    lines.append("")
    lines.append("二、还可能欠休 / 观察不足（只列调查，不生成补休承诺）")
    if not possible and not under:
        lines.append("  无。")
    for finding in possible + under:
        lines.append(
            f"  {finding.finding_id} {finding.person}（{finding.group}）"
            f"{STATUS_TEXT[finding.status]} 条款 {'+'.join(finding.rule_ids)}："
            f"休息下界 {finding.rest_lower // MINUTE} 分钟 / 上界 {finding.rest_upper // MINUTE} 分钟"
            f" / 要求 {finding.required // MINUTE} 分钟，观察覆盖 {finding.coverage:.0%}"
        )
        sources = sorted({s for dispute in finding.disputes for s in dispute.sources})
        if sources:
            lines.append(f"      证据来源：{'、'.join(sources)}")
        if finding.status == UNDERDETERMINED:
            lines.append("      签到覆盖不足以下结论，需补录或现场核验，暂不产生补休。")

    lines.append("")
    lines.append("三、比较分组（同一把尺：窗长、协议夜间窗、观察覆盖）")
    lines.append(
        "  组别   观察窗  已判定  已确认  可能  未判定  观察覆盖  夜间条款窗  夜间覆盖"
        "  休息下界  休息上界  按义务推定  确认率"
    )
    for group in sorted(result.groups.values(), key=lambda g: g.group):
        lines.append(
            f"  {group.group:<5} {group.windows:>5} {group.evaluable:>6} {group.confirmed:>6}"
            f" {group.possible:>5} {group.underdetermined:>6} {group.coverage:>8.0%}"
            f" {group.night_windows:>10} {group.night_coverage:>8.0%}"
            f" {hhmm(group.average_rest):>9} {hhmm(group.average_rest_upper):>9}"
            f" {hhmm(group.average_rest_presumed):>10}"
            f" {group.confirmed_rate:>7.0%}"
        )
    low_cover = [g for g in result.groups.values() if g.coverage and g.coverage < 0.75]
    if low_cover:
        lines.append(
            "  注意："
            + "、".join(f"{g.group}（{g.coverage:.0%}）" for g in low_cover)
            + " 观察覆盖偏低。其“未确认”不等于安全，不能据此判断哪组更安全。"
        )
    if len(result.groups) > 1:
        best = max(result.groups.values(), key=lambda g: g.confirmed_rate)
        worst = min(result.groups.values(), key=lambda g: g.confirmed_rate)
        if best.evaluable and best.confirmed_rate > worst.confirmed_rate:
            lines.append(
                f"  异常集中：{best.group} 组已确认欠休比例 {best.confirmed_rate:.0%}，"
                f"高于 {worst.group} 组 {worst.confirmed_rate:.0%}"
                f"（{best.confirmed}/{best.evaluable} 对 {worst.confirmed}/{worst.evaluable}）；"
                f"观察覆盖分别为 {best.coverage:.0%} / {worst.coverage:.0%}。"
                "比例差异需结合覆盖解释，覆盖低的一组可能只是缺记录。"
            )
        by_presumed = max(result.groups.values(), key=lambda g: g.average_rest_presumed)
        by_evidence = max(result.groups.values(), key=lambda g: g.average_rest)
        by_upper = max(result.groups.values(), key=lambda g: g.average_rest_upper)
        # 上界高于按义务推定的部分，正是“缺签到被当成休息”带来的虚增。
        inflated = {
            g.group: g.average_rest_upper - g.average_rest_presumed
            for g in result.groups.values()
        }
        worst_inflation = max(inflated, key=lambda name: inflated[name])
        if inflated[worst_inflation] > 0:
            lines.append(
                f"  口径提示：若把无记录时间当作休息，{worst_inflation} 组平均会虚增 "
                f"{hhmm(inflated[worst_inflation])}。已证下界排序 {by_evidence.group} 最高、"
                f"上界排序 {by_upper.group} 最高、按义务推定 {by_presumed.group} 最高；"
                "排序不一致时说明签到缺失正在改变表面结论，不能只看上界或单一均值。"
            )
        elif len({by_presumed.group, by_evidence.group}) > 1:
            lines.append(
                f"  口径提示：按已证休息与按签定义务推定的排序不同"
                f"（{by_evidence.group} / {by_presumed.group}），复核应结合观察覆盖判断。"
            )

    lines.append("")
    lines.append("四、补休积欠")
    if not result.arrears:
        lines.append("  无。")
    for item in result.arrears:
        text = (
            f"  {item.finding_id} {item.person} {item.state}"
            f" 范围 {format_instant(item.scope.start, tz)}→{format_instant(item.scope.stop, tz)}"
        )
        if item.owed_minutes:
            text += f" 尚欠 {item.owed_minutes} 分钟"
        if item.promise:
            text += f" 已签收 {item.promise} {item.scheduled_minutes} 分钟"
        lines.append(text)

    lines.append("")
    lines.append("五、签到缺失（缺失不得按休息计）")
    if not result.clock_gaps:
        lines.append("  无。")
    for gap in result.clock_gaps:
        lines.append(
            f"  {gap['person']} {gap['duty'] or '—'}（{gap['site'] or '—'}）"
            f" {format_instant(gap['interval'].start, tz)}→{format_instant(gap['interval'].stop, tz)}"
            f" 少 {gap['minutes']} 分钟"
        )

    if result.person_notes:
        lines.append("")
        lines.append("六、证据说明")
        for person in sorted(result.person_notes):
            for note in result.person_notes[person]:
                lines.append(f"  {person}：{note}")
    return "\n".join(lines)


def as_dict(result: Review, tz: str = "America/New_York") -> dict:
    def disputes(items: Iterable[Dispute]) -> List[dict]:
        return [
            {
                "start": format_instant(item.interval.start, tz),
                "stop": format_instant(item.interval.stop, tz),
                "reason": item.reason,
                "sources": list(item.sources),
            }
            for item in items
        ]

    return {
        "windows": [
            {
                "person": w.person,
                "group": w.group,
                "duty": w.duty_id,
                "rule": w.rule.rule,
                "start": format_instant(w.window.start, tz),
                "stop": format_instant(w.window.stop, tz),
                "required_minutes": w.required // MINUTE,
                "rest_lower_minutes": w.rest_lower // MINUTE,
                "rest_upper_minutes": w.rest_upper // MINUTE,
                "rest_presumed_minutes": w.rest_presumed // MINUTE,
                "obligation_minutes": w.obligation // MINUTE,
                "observed_minutes": w.observed // MINUTE,
                "coverage": round(w.coverage, 4),
                "status": w.status,
            }
            for w in result.windows
        ],
        "findings": [
            {
                "id": f.finding_id,
                "person": f.person,
                "group": f.group,
                "status": f.status,
                "rules": f.rule_ids,
                "duties": f.duties,
                "scope_start": format_instant(f.scope.start, tz),
                "scope_stop": format_instant(f.scope.stop, tz),
                "required_minutes": f.required // MINUTE,
                "rest_lower_minutes": f.rest_lower // MINUTE,
                "rest_upper_minutes": f.rest_upper // MINUTE,
                "coverage": round(f.coverage, 4),
                "dedup_group": f.dedup_group,
                "compensation_minutes": f.compensation_minutes,
                "promise": f.promise,
                "scheduled_minutes": f.scheduled_minutes,
                "outstanding_minutes": f.outstanding_minutes,
                "disputes": disputes(f.disputes),
            }
            for f in result.findings
        ],
        "groups": [
            {
                "group": g.group,
                "windows": g.windows,
                "evaluable": g.evaluable,
                "met": g.met,
                "confirmed": g.confirmed,
                "possible": g.possible,
                "underdetermined": g.underdetermined,
                "coverage": round(g.coverage, 4),
                "night_windows": g.night_windows,
                "night_coverage": round(g.night_coverage, 4),
                "confirmed_rate": round(g.confirmed_rate, 4),
                "average_rest_minutes": g.average_rest // MINUTE,
                "presumed_rest_minutes": (g.rest_presumed_seconds // max(g.windows, 1)) // MINUTE,
                "confirmed_shortfall_minutes": g.confirmed_shortfall_seconds // MINUTE,
            }
            for g in sorted(result.groups.values(), key=lambda g: g.group)
        ],
        "arrears": [
            {
                "id": a.finding_id,
                "person": a.person,
                "group": a.group,
                "status": a.status,
                "state": a.state,
                "scope_start": format_instant(a.scope.start, tz),
                "scope_stop": format_instant(a.scope.stop, tz),
                "owed_minutes": a.owed_minutes,
                "scheduled_minutes": a.scheduled_minutes,
                "promise": a.promise,
            }
            for a in result.arrears
        ],
        "clock_gaps": [
            {
                "person": g["person"],
                "duty": g["duty"],
                "site": g["site"],
                "start": format_instant(g["interval"].start, tz),
                "stop": format_instant(g["interval"].stop, tz),
                "minutes": g["minutes"],
            }
            for g in result.clock_gaps
        ],
        "notes": result.person_notes,
    }
