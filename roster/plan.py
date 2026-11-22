"""候选班表试算与确认。

一套候选班表包含：
- assignments：班次由谁值守；
- breaks：班内排出的自由休息，可指定替班；
- compensations：兑现旧欠休的休息窗口，可指定替班；
- reliefs：额外增加的替班义务窗口。

试算同时报告：必守岗位是否失去覆盖、谁跌破连续休息底线、旧欠休可兑现多少、
平均休息的变化可能掩盖哪些个体违规。替班本身会占用替班人的义务时间，
因此替班可能截断的是另一个人的连续休息。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import Materials, NextShift
from .review import Review
from .timeutil import (
    MINUTE,
    Interval,
    clip,
    format_instant,
    hhmm,
    longest,
    normalize,
    parse_instant,
    subtract,
    total,
)

KIND_CANDIDATE = "candidate"
KIND_LOAN = "loan"


@dataclass
class Assignment:
    shift: str
    person: str
    kind: str = KIND_CANDIDATE
    note: str = ""


@dataclass
class FreeWindow:
    start: int
    stop: int
    shift: Optional[str] = None
    relief: Optional[str] = None
    finding: Optional[str] = None
    note: str = ""

    @property
    def span(self) -> Interval:
        return Interval(self.start, self.stop)


@dataclass
class Relief:
    person: str
    start: int
    stop: int
    site: str
    role: str
    reason: str = ""

    @property
    def span(self) -> Interval:
        return Interval(self.start, self.stop)


@dataclass
class Plan:
    name: str
    assignments: Dict[str, Assignment] = field(default_factory=dict)
    breaks: List[FreeWindow] = field(default_factory=list)
    compensations: List[FreeWindow] = field(default_factory=list)
    reliefs: List[Relief] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        tz = "America/New_York"
        return {
            "name": self.name,
            "assignments": {
                key: {"person": item.person, "kind": item.kind, "note": item.note}
                for key, item in sorted(self.assignments.items())
            },
            "breaks": [_window_dict(item, tz) for item in self.breaks],
            "compensations": [
                dict(_window_dict(item, tz), finding=item.finding) for item in self.compensations
            ],
            "reliefs": [
                {
                    "person": item.person,
                    "start": format_instant(item.start, tz),
                    "stop": format_instant(item.stop, tz),
                    "site": item.site,
                    "role": item.role,
                    "reason": item.reason,
                }
                for item in self.reliefs
            ],
            "notes": list(self.notes),
        }


def _window_dict(item: FreeWindow, tz: str) -> dict:
    return {
        "start": format_instant(item.start, tz),
        "stop": format_instant(item.stop, tz),
        "shift": item.shift,
        "relief": item.relief,
        "note": item.note,
    }


def load_plan(text: str, tz: str = "America/New_York") -> Plan:
    payload = json.loads(text)
    plan = Plan(name=payload.get("name", "未命名"))
    for shift, item in payload.get("assignments", {}).items():
        if isinstance(item, str):
            plan.assignments[shift] = Assignment(shift, item)
        else:
            plan.assignments[shift] = Assignment(
                shift, item["person"], item.get("kind", KIND_CANDIDATE), item.get("note", "")
            )
    for item in payload.get("breaks", []):
        plan.breaks.append(
            FreeWindow(
                start=parse_instant(item["start"], tz),
                stop=parse_instant(item["stop"], tz),
                shift=item.get("shift"),
                relief=item.get("relief"),
                note=item.get("note", ""),
            )
        )
    for item in payload.get("compensations", []):
        plan.compensations.append(
            FreeWindow(
                start=parse_instant(item["start"], tz),
                stop=parse_instant(item["stop"], tz),
                finding=item.get("finding"),
                relief=item.get("relief"),
                note=item.get("note", ""),
            )
        )
    for item in payload.get("reliefs", []):
        plan.reliefs.append(
            Relief(
                person=item["person"],
                start=parse_instant(item["start"], tz),
                stop=parse_instant(item["stop"], tz),
                site=item["site"],
                role=item["role"],
                reason=item.get("reason", ""),
            )
        )
    plan.notes = list(payload.get("notes", []))
    return plan


@dataclass
class PersonPlan:
    person: str
    obligations: List[Interval] = field(default_factory=list)
    free_blocks: List[Interval] = field(default_factory=list)
    shifts: List[str] = field(default_factory=list)
    reliefs: int = 0

    def rest_intervals(self, horizon: Optional[Interval] = None) -> List[Interval]:
        obligations = normalize(self.obligations)
        spans = obligations + list(self.free_blocks)
        if not spans:
            return []
        if horizon is None:
            start = min(item.start for item in spans)
            stop = max(item.stop for item in spans)
        else:
            start, stop = horizon.start, horizon.stop
        return normalize(subtract([Interval(start, stop)], obligations) + self.free_blocks)


@dataclass
class ShiftResult:
    shift: NextShift
    person: Optional[str]
    kind: str
    qualified: bool
    covered: bool
    contested: bool
    gaps: List[Interval]
    reason: str = ""


@dataclass
class RestCheck:
    person: str
    shift: str
    rule: str
    window: Interval
    required: int
    achieved: int
    met: bool
    presumed: bool = False
    relief_caused: bool = False
    without_relief: int = 0
    back_to_back: bool = False


@dataclass
class PlanResult:
    plan: Plan
    shifts: List[ShiftResult]
    rests: List[RestCheck]
    compensation_rows: List[dict]
    delivered_minutes: int
    owed_total: int
    owed_remaining: int
    avoidable_minutes: int
    unavoidable_minutes: int
    coverage_losses: List[ShiftResult]
    person_rest: Dict[str, dict]
    group_average: Dict[str, int]
    warnings: List[str]

    @property
    def violations(self) -> List[RestCheck]:
        return [item for item in self.rests if not item.met]

    @property
    def violating_persons(self) -> List[str]:
        return sorted({item.person for item in self.violations})

    @property
    def new_shortfall_minutes(self) -> int:
        """本方案本身造成的连续休息缺口，与旧欠账分开。"""
        return sum(item.required - item.achieved for item in self.violations) // MINUTE


def evaluate(materials: Materials, review: Review, plan: Plan) -> PlanResult:
    warnings: List[str] = []
    shifts = sorted(materials.next_shifts.values(), key=lambda s: (s.start, s.shift))
    finding_by_id = {f.finding_id: f for f in review.confirmed}

    for shift_id, assignment in plan.assignments.items():
        if shift_id not in materials.next_shifts:
            warnings.append(f"候选班表引用了未知班次 {shift_id}")
            continue
        shift = materials.next_shifts[shift_id]
        operator = materials.operator(assignment.person)
        if operator is None:
            warnings.append(f"{shift_id} 指定了未知职工 {assignment.person}")
        elif not operator.qualifies(shift.role):
            warnings.append(f"{shift_id} 要求 {shift.role} 资质，{assignment.person} 不具备")

    per_person: Dict[str, PersonPlan] = {person: PersonPlan(person=person) for person in materials.operators}

    def line(person: str) -> PersonPlan:
        return per_person.setdefault(person, PersonPlan(person=person))

    # 义务：班次、显式替班、为他人休息顶班。
    window_owner: List[Tuple[str, FreeWindow]] = []
    for item in plan.breaks:
        assignment = plan.assignments.get(item.shift or "")
        if assignment is None:
            warnings.append(f"班内休息 {item.span} 找不到对应班次，已忽略")
            continue
        window_owner.append((assignment.person, item))
    for item in plan.compensations:
        finding = finding_by_id.get(item.finding or "")
        if finding is None:
            warnings.append(f"补休窗口指向未知或未确认的工单 {item.finding}，已忽略")
            continue
        window_owner.append((finding.person, item))

    for item in window_owner:
        owner, window = item
        line(owner).free_blocks.append(window.span)
        if window.relief:
            line(window.relief).obligations.append(window.span)
            line(window.relief).reliefs += 1
            operator = materials.operator(window.relief)
            shift = materials.next_shifts.get(window.shift or "")
            if shift and (operator is None or not operator.qualifies(shift.role)):
                warnings.append(
                    f"替班 {window.relief} 不具备 {shift.shift} 的 {shift.role} 资质"
                )
            # 顶班者若在同一时刻已被排了另一个班，等于同一人被排到两处。
            clash = [
                other.shift
                for other in shifts
                if plan.assignments.get(other.shift) is not None
                and plan.assignments[other.shift].person == window.relief
                and other.span.overlaps(window.span)
            ]
            clash = [name for name in clash if name != window.shift]
            if clash:
                warnings.append(
                    f"替班 {window.relief} 在 {window.span.start}–{window.span.stop} "
                    f"同时被排到 {'、'.join(clash)}，替班不成立"
                )

    for shift in shifts:
        assignment = plan.assignments.get(shift.shift)
        if assignment is not None:
            line(assignment.person).obligations.append(shift.span)
            line(assignment.person).shifts.append(shift.shift)
    for relief in plan.reliefs:
        line(relief.person).obligations.append(relief.span)
        line(relief.person).reliefs += 1
        if materials.operator(relief.person) is None or not materials.operator(relief.person).qualifies(relief.role):
            warnings.append(f"额外替班 {relief.person} 不具备 {relief.site}/{relief.role} 资质")

    # -- 岗位覆盖 -----------------------------------------------------
    shift_results: List[ShiftResult] = []
    for shift in shifts:
        assignment = plan.assignments.get(shift.shift)
        if assignment is None:
            shift_results.append(ShiftResult(shift, None, "", False, False, False, [shift.span], "未安排人员"))
            continue
        operator = materials.operator(assignment.person)
        qualified = bool(operator and operator.qualifies(shift.role))
        same_person_elsewhere = [
            other
            for other in shifts
            if other.shift != shift.shift
            and plan.assignments.get(other.shift) is not None
            and plan.assignments[other.shift].person == assignment.person
            and other.span.overlaps(shift.span)
        ]
        own_holes: List[Interval] = []
        for owner, window in window_owner:
            if owner != assignment.person:
                continue
            piece = window.span.intersect(shift.span)
            if piece is None:
                continue
            relief_operator = materials.operator(window.relief) if window.relief else None
            if relief_operator and relief_operator.qualifies(shift.role):
                continue
            own_holes.append(piece)
        # 额外替班只覆盖明确声明的岗位，不自动替班内休息。
        extra = [
            relief.span
            for relief in plan.reliefs
            if relief.site == shift.site
            and relief.role == shift.role
            and relief.person != assignment.person
        ]
        gaps = subtract(normalize(own_holes), extra)
        contested = bool(same_person_elsewhere)
        covered = qualified and not contested and not gaps
        reason = ""
        if not qualified:
            reason = "不具备岗位资质"
        elif contested:
            reason = "同一人同一时刻被排到多个岗位"
        elif gaps:
            reason = "休息/补休窗口无人替班"
        shift_results.append(
            ShiftResult(
                shift,
                assignment.person,
                assignment.kind,
                qualified,
                qualified and not contested and not gaps,
                contested,
                gaps,
                reason,
            )
        )

    # -- 连续休息 -----------------------------------------------------
    horizon = Interval(min(s.start for s in shifts), max(s.stop for s in shifts))
    rests: List[RestCheck] = []
    for shift in shifts:
        assignment = plan.assignments.get(shift.shift)
        if assignment is None:
            continue
        person = assignment.person
        free = line(person).rest_intervals(horizon)
        # 没有替班义务时，这个人的可自由时间会是多少：用来判断违规是否由替班造成。
        without_relief = PersonPlan(
            person=person,
            obligations=[span for span in line(person).obligations if span not in _relief_spans(plan, person)],
            free_blocks=list(line(person).free_blocks),
        ).rest_intervals(horizon)
        for rule in materials.rules_for(person):
            window = Interval(shift.start, shift.start + rule.window_seconds)
            achieved = longest(clip(free, window.start, window.stop))
            baseline = longest(clip(without_relief, window.start, window.stop))
            failed = achieved < rule.required_free_seconds
            # 本人被连排或替班占用都可能造成缺口，分别归因，避免把排班冲突说成替班代价。
            own_conflict = any(
                other.shift != shift.shift
                and plan.assignments.get(other.shift) is not None
                and plan.assignments[other.shift].person == person
                and other.span.overlaps(window)
                for other in shifts
            )
            rests.append(
                RestCheck(
                    person=person,
                    shift=shift.shift,
                    rule=rule.rule,
                    window=window,
                    required=rule.required_free_seconds,
                    achieved=achieved,
                    met=not failed,
                    relief_caused=failed and baseline >= rule.required_free_seconds,
                    without_relief=baseline,
                    back_to_back=failed and own_conflict,
                )
            )

    # -- 补休兑现 -----------------------------------------------------
    compensation_rows: List[dict] = []
    delivered_by_finding: Dict[str, int] = {}
    for owner, window in window_owner:
        if window.finding is None:
            continue
        finding = finding_by_id.get(window.finding)
        allowed = finding.compensation_minutes if finding else 0
        # 兑现窗口必须真的是本人可自由支配的时间：不能压在本人其他班次上。
        clash = any(
            obligation.overlaps(window.span)
            for obligation in normalize(line(owner).obligations)
            if not any(window.span == block for block in line(owner).free_blocks)
        )
        # 若窗口与本人班次重叠，则必须由替班覆盖该班次才算兑现。
        covered_by_relief = False
        if clash and window.relief:
            covered_by_relief = True
        deliverable = not clash or covered_by_relief
        minutes = min(window.span.duration // MINUTE, allowed) if deliverable else 0
        delivered_by_finding[window.finding] = delivered_by_finding.get(window.finding, 0) + minutes
        compensation_rows.append(
            {
                "finding": window.finding,
                "person": owner,
                "start": window.start,
                "stop": window.stop,
                "relief": window.relief,
                "minutes": minutes,
                "deliverable": deliverable,
                "reason": "" if deliverable else "与本人其他班次重叠且无替班",
            }
        )

    owed_total = 0
    owed_remaining = 0
    for finding in review.confirmed:
        owed = finding.compensation_minutes
        covered = min(owed, finding.scheduled_minutes + delivered_by_finding.get(finding.finding_id, 0))
        owed_total += owed
        owed_remaining += max(0, owed - covered)

    # -- 可避免 / 不可避免 --------------------------------------------
    # 可避免 = 排班员有排班自由却没有排上的旧欠休；不可避免 = 排班期内无空档可兑现。
    horizon = Interval(min(s.start for s in shifts), max(s.stop for s in shifts))
    avoidable = 0
    unavoidable = 0
    for finding in review.confirmed:
        owed = finding.compensation_minutes
        covered = min(owed, finding.scheduled_minutes + delivered_by_finding.get(finding.finding_id, 0))
        need = max(0, owed - covered)
        if not need:
            continue
        # 已签收的补休先占位，不计入可自由安排的额度。
        if _feasible(materials, per_person.get(finding.person), finding.person, need, horizon):
            avoidable += need
        else:
            unavoidable += need

    person_rest: Dict[str, dict] = {}
    for person, item in per_person.items():
        intervals = item.rest_intervals(horizon)
        person_rest[person] = {
            "longest_minutes": longest(intervals) // MINUTE,
            "obligation_minutes": total(item.obligations) // MINUTE,
            "reliefs": item.reliefs,
        }
    grouped: Dict[str, List[int]] = {}
    for person, row in person_rest.items():
        if not row["obligation_minutes"]:
            continue
        grouped.setdefault(materials.group_of(person), []).append(row["longest_minutes"])
    group_average = {group: sum(values) // len(values) for group, values in grouped.items()}

    return PlanResult(
        plan=plan,
        shifts=shift_results,
        rests=rests,
        compensation_rows=compensation_rows,
        delivered_minutes=sum(row["minutes"] for row in compensation_rows),
        owed_total=owed_total,
        owed_remaining=owed_remaining,
        avoidable_minutes=avoidable,
        unavoidable_minutes=unavoidable,
        coverage_losses=[item for item in shift_results if item.shift.mandatory and not item.covered],
        person_rest=person_rest,
        group_average=group_average,
        warnings=warnings,
    )


def _relief_spans(plan: Plan, person: str) -> List[Interval]:
    spans = [item.span for item in plan.reliefs if item.person == person]
    for item in plan.breaks + plan.compensations:
        if item.relief == person:
            spans.append(item.span)
    return spans


def _feasible(
    materials: Materials,
    line: Optional[PersonPlan],
    person: str,
    need_minutes: int,
    horizon: Interval,
) -> Optional[Interval]:
    """在排班期内为某人找一段不压义务的休息窗口。"""
    taken: List[Interval] = list(line.obligations) if line else []
    for promise in materials.rest_promises.values():
        if promise.person == person:
            taken.append(promise.span)
    gaps = subtract([horizon], taken)
    for gap in gaps:
        if gap.duration >= need_minutes * MINUTE:
            return Interval(gap.start, gap.start + need_minutes * MINUTE)
    return None


# -- 候选班表生成 ---------------------------------------------------------


def _eligible(materials: Materials, shift: NextShift, person: str) -> bool:
    operator = materials.operator(person)
    return bool(operator and operator.qualifies(shift.role))


def _promised(materials: Materials, person: str, span: Interval) -> bool:
    return any(
        promise.person == person and promise.span.overlaps(span)
        for promise in materials.rest_promises.values()
    )


def _greedy_assign(
    materials: Materials,
    shifts: Sequence[NextShift],
    taken: Dict[str, List[Interval]],
    pool: Optional[Sequence[str]] = None,
) -> Tuple[Dict[str, Assignment], List[str]]:
    assignments: Dict[str, Assignment] = {}
    notes: List[str] = []
    for shift in shifts:
        chosen: Optional[str] = None
        candidates = list(pool) if pool is not None else list(shift.candidates)
        for person in candidates:
            if not _eligible(materials, shift, person):
                continue
            if _promised(materials, person, shift.span):
                continue
            if any(interval.overlaps(shift.span) for interval in taken.get(person, [])):
                continue
            chosen = person
            break
        if chosen is None:
            notes.append(f"{shift.shift}（{shift.site}/{shift.role}）无可用候选")
            continue
        assignments[shift.shift] = Assignment(shift.shift, chosen)
        taken.setdefault(chosen, []).append(shift.span)
    return assignments, notes


def _break_window(shift: NextShift, minutes: int = 50) -> Interval:
    middle = shift.start + (shift.stop - shift.start) // 2
    start = middle - (minutes * MINUTE) // 2
    return Interval(start, start + minutes * MINUTE)


def generate_variants(materials: Materials, review: Review) -> List[Plan]:
    shifts = sorted(materials.next_shifts.values(), key=lambda s: (s.start, s.shift))
    promises_taken: Dict[str, List[Interval]] = {
        person: [p.span for p in materials.rest_promises.values() if p.person == person]
        for person in materials.operators
    }
    plans: List[Plan] = []

    # A. 不排休息：只排人。
    taken: Dict[str, List[Interval]] = {p: list(v) for p, v in promises_taken.items()}
    base, notes_a = _greedy_assign(materials, shifts, taken)
    plans.append(Plan(name="A-不排休息", assignments=dict(base), notes=list(notes_a)))

    # B. 只排有替班的休息：没有替班就不排，保留缺口。
    taken_b: Dict[str, List[Interval]] = {p: list(v) for p, v in promises_taken.items()}
    assignments_b, notes_b = _greedy_assign(materials, shifts, taken_b)
    breaks_b: List[FreeWindow] = []
    extra_b: List[str] = []
    for shift in shifts:
        assignment = assignments_b.get(shift.shift)
        if assignment is None:
            continue
        window = _break_window(shift)
        relief = None
        for person in shift.candidates:
            if person == assignment.person or not _eligible(materials, shift, person):
                continue
            if _promised(materials, person, window):
                continue
            if any(interval.overlaps(window) for interval in taken_b.get(person, [])):
                continue
            relief = person
            break
        if relief is None and shift.mandatory:
            extra_b.append(
                f"{shift.shift}：休息期间无合格替班，未排休息，保留连续休息缺口而不虚构覆盖。"
            )
            continue
        if relief is not None:
            taken_b.setdefault(relief, []).append(window)
        breaks_b.append(FreeWindow(window.start, window.stop, shift=shift.shift, relief=relief))
    plans.append(
        Plan(
            name="B-只排有替班的休息",
            assignments=assignments_b,
            breaks=breaks_b,
            notes=list(notes_b) + extra_b,
        )
    )

    # C. 借调补位：候选名单之外的人补上空缺，并尽量排休。
    taken_c: Dict[str, List[Interval]] = {p: list(v) for p, v in promises_taken.items()}
    assignments_c, notes_c = _greedy_assign(materials, shifts, taken_c)
    extra_c: List[str] = []
    for shift in shifts:
        if shift.shift in assignments_c:
            continue
        chosen = None
        for person in materials.operators:
            if not _eligible(materials, shift, person):
                continue
            if _promised(materials, person, shift.span):
                continue
            if any(interval.overlaps(shift.span) for interval in taken_c.get(person, [])):
                continue
            chosen = person
            break
        if chosen is None:
            extra_c.append(f"{shift.shift}：无合格人选，必守岗位失去覆盖。")
            continue
        assignments_c[shift.shift] = Assignment(shift.shift, chosen, KIND_LOAN, "候选名单外借调")
        taken_c.setdefault(chosen, []).append(shift.span)
        extra_c.append(f"{shift.shift}：借调 {chosen} 补位。")

    breaks_c: List[FreeWindow] = []
    for shift in shifts:
        assignment = assignments_c.get(shift.shift)
        if assignment is None:
            continue
        window = _break_window(shift)
        relief = None
        for person, operator in materials.operators.items():
            if person == assignment.person or not operator.qualifies(shift.role):
                continue
            if _promised(materials, person, window):
                continue
            if any(interval.overlaps(window) for interval in taken_c.get(person, [])):
                continue
            relief = person
            break
        if relief is None:
            extra_c.append(f"{shift.shift}：{assignment.person} 的休息无替班，覆盖出现缺口。")
            continue
        taken_c.setdefault(relief, []).append(window)
        breaks_c.append(FreeWindow(window.start, window.stop, shift=shift.shift, relief=relief))
        extra_c.append(f"{shift.shift}：{assignment.person} 班内休息，{relief} 替班。")
    plans.append(
        Plan(
            name="C-借调补位",
            assignments=assignments_c,
            breaks=breaks_c,
            notes=list(notes_c) + extra_c,
        )
    )

    # D. 在 C 基础上兑现旧欠休。已签收的补休继续占用其本人，不能被排班；
    #    同一职工的多笔欠休要排在不同时段，避免同一小时被重复兑现。
    horizon = Interval(min(s.start for s in shifts), max(s.stop for s in shifts))
    compensations_d: List[FreeWindow] = []
    notes_d: List[str] = []
    used_by_person: Dict[str, List[Interval]] = {
        person: list(taken_c.get(person, [])) for person in materials.operators
    }
    for owner, window in _owned_windows(assignments_c, breaks_c):
        used_by_person.setdefault(owner, []).append(window.span)
    for finding in review.confirmed:
        need = max(0, finding.compensation_minutes - finding.scheduled_minutes)
        if not need:
            continue
        taken = used_by_person.setdefault(finding.person, [])
        # 已签收补休先占位；若工单已由该承诺结清，need 已为 0，不会重复兑现。
        taken.extend(
            promise.span for promise in materials.rest_promises.values() if promise.person == finding.person
        )
        line = PersonPlan(person=finding.person, obligations=list(taken))
        slot = _feasible(materials, line, finding.person, need, horizon)
        if slot is None:
            notes_d.append(f"{finding.finding_id} {finding.person}：排班期内找不到可兑现窗口。")
            continue
        # 与已签收的承诺时间重叠也不重复安排。
        if any(
            slot.overlaps(promise.span)
            for promise in materials.rest_promises.values()
            if promise.person == finding.person
        ):
            notes_d.append(
                f"{finding.finding_id} {finding.person}：可兑现窗口与已签收补休重叠，不再重复安排。"
            )
            continue
        taken.append(slot)
        compensations_d.append(
            FreeWindow(slot.start, slot.stop, finding=finding.finding_id, note="兑现旧欠休")
        )
        notes_d.append(f"{finding.finding_id} {finding.person}：安排补休 {need} 分钟。")
    plans.append(
        Plan(
            name="D-补休兑现",
            assignments=dict(assignments_c),
            breaks=list(breaks_c),
            compensations=compensations_d,
            notes=list(notes_c) + extra_c + notes_d,
        )
    )
    return plans


def _owned_windows(
    assignments: Dict[str, Assignment],
    breaks: Sequence[FreeWindow],
) -> List[Tuple[str, FreeWindow]]:
    """班内休息归属于该班次当前的排班人。"""
    rows: List[Tuple[str, FreeWindow]] = []
    for item in breaks:
        assignment = assignments.get(item.shift or "")
        if assignment:
            rows.append((assignment.person, item))
    return rows


# -- 呈现 -----------------------------------------------------------------


def render_result(materials: Materials, result: PlanResult, tz: str = "America/New_York") -> str:
    lines: List[str] = []
    lines.append(f"候选班表 {result.plan.name}")
    lines.append("=" * 8)
    lines.append(
        f"必守岗位失去覆盖 {len(result.coverage_losses)} 个；"
        f"未达连续休息 {len(result.violations)} 处（涉及 {len(result.violating_persons)} 人）；"
        f"可避免欠休 {result.avoidable_minutes} 分钟，不可避免 {result.unavoidable_minutes} 分钟；"
        f"补休已兑现 {result.delivered_minutes} 分钟，旧账仍欠 {result.owed_remaining} 分钟。"
    )
    if result.warnings:
        lines.append("注意：")
        for warning in result.warnings:
            lines.append(f"  - {warning}")

    lines.append("")
    lines.append("班次与覆盖")
    for item in result.shifts:
        shift = item.shift
        label = "必守" if shift.mandatory else "非必守"
        if item.covered:
            state = "覆盖"
        elif not item.qualified:
            state = "不可用"
        elif item.contested:
            state = "争议"
        else:
            state = "缺口"
        lines.append(
            f"  {shift.shift} {shift.site}/{shift.role}（{label}）{item.person or '—'}"
            f" [{item.kind or '未安排'}] {state}" + (f"：{item.reason}" if item.reason else "")
        )
        for gap in item.gaps:
            lines.append(
                f"      空档 {format_instant(gap.start, tz)}→{format_instant(gap.stop, tz)}"
                f"（{gap.duration // MINUTE} 分钟）"
            )

    lines.append("")
    lines.append("连续休息核对（观察窗自班次起点计）")
    for item in result.rests:
        flag = "达标" if item.met else "不足"
        if item.relief_caused:
            cause = "，由替班占用造成"
        elif item.back_to_back:
            cause = "，因本人被连排两个班"
        else:
            cause = ""
        lines.append(
            f"  {item.person} {item.shift} {item.rule} {flag}："
            f"最长连续自由 {item.achieved // MINUTE} / 要求 {item.required // MINUTE} 分钟{cause}"
        )

    lines.append("")
    lines.append("补休兑现")
    if not result.compensation_rows:
        lines.append("  未安排补休。")
    for row in result.compensation_rows:
        state = "可兑现" if row["deliverable"] else "不可兑现"
        lines.append(
            f"  {row['finding']} {row['person']} {state}"
            f" {format_instant(row['start'], tz)}→{format_instant(row['stop'], tz)}"
            f" {row['minutes']} 分钟"
            + (f"，替班 {row['relief']}" if row["relief"] else "")
            + (f"（{row['reason']}）" if row["reason"] else "")
        )

    lines.append("")
    lines.append("个人与分组休息")
    for person, row in sorted(result.person_rest.items()):
        if not row["obligation_minutes"]:
            continue
        lines.append(
            f"  {person}（{materials.group_of(person)}）义务 {hhmm(row['obligation_minutes'] * MINUTE)}"
            f" 最长连续休息 {hhmm(row['longest_minutes'] * MINUTE)}"
            f"{' 替班 ' + str(row['reliefs']) + ' 次' if row['reliefs'] else ''}"
        )
    for group, value in sorted(result.group_average.items()):
        lines.append(f"  {group} 组平均最长休息 {hhmm(value * MINUTE)}")
    if result.violating_persons:
        lines.append("  注意：平均休息不能替代个体底线，以下人员仍未达标：")
        for person in result.violating_persons:
            worst = min(
                (item for item in result.violations if item.person == person),
                key=lambda i: i.achieved - i.required,
            )
            if worst.relief_caused:
                cause = (
                    f"；若不承担替班可达 {worst.without_relief // MINUTE} 分钟，"
                    "缺口由替班占用造成"
                )
            elif worst.back_to_back:
                cause = "；缺口因本人被连排两个班"
            else:
                cause = "；班内未排出可自由支配的休息（或无合格替班可排）"
            lines.append(
                f"    {person}（{worst.shift}）差 {(worst.required - worst.achieved) // MINUTE} 分钟{cause}"
            )
    return "\n".join(lines)


def compare(results: Sequence[PlanResult], tz: str = "America/New_York") -> str:
    lines: List[str] = []
    lines.append("候选班表比较")
    lines.append("=" * 8)
    lines.append(
        "  方案                必守缺口  休息不足  涉及人数  可避免  不可避免  已兑现  仍欠  组平均最长休息"
    )
    for result in results:
        averages = " / ".join(
            f"{group} {hhmm(value * MINUTE)}" for group, value in sorted(result.group_average.items())
        )
        lines.append(
            f"  {result.plan.name:<18} {len(result.coverage_losses):>6} {len(result.violations):>8}"
            f" {len(result.violating_persons):>8} {result.avoidable_minutes:>6}"
            f" {result.unavoidable_minutes:>8} {result.delivered_minutes:>7}"
            f" {result.owed_remaining:>5}  {averages}"
        )
    lines.append("")
    lines.append("平均最长休息上升时，仍要对照个体底线与必守岗位覆盖，不能只看均值。")
    return "\n".join(lines)
