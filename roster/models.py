"""资料表的读取与实体封装。

读取时保留原始字段，同时把时间折成 UTC 秒。任何一行结构不符即整体报错，
以免半份资料得出看似完整的结论。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from .timeutil import Interval, parse_instant, parse_optional

DEFAULT_TIMEZONE = "America/New_York"

CLOCK_STATES = {"onsite", "standby"}
BREAK_EVIDENCE = {"free", "standby", "recall"}
LOAN_EVIDENCE = {"confirmed", "conflict"}


class MaterialError(ValueError):
    """资料无法读取或语义不合法。"""


def read_tables(directory: Path) -> Dict[str, List[dict]]:
    """按 manifest 读取全部 JSON Lines 表。"""
    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tables: Dict[str, List[dict]] = {}
    for name, columns in manifest.items():
        expected = set(columns)
        path = directory / f"{name}.jsonl"
        records: List[dict] = []
        with path.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict) or record.keys() != expected:
                    raise MaterialError(f"{name} 第 {number} 行的字段与登记格式不符")
                records.append(record)
        tables[name] = records
    for required in ("operators", "duties", "clock", "loans", "breaks", "agreements"):
        if required not in tables:
            raise MaterialError(f"缺少资料表 {required}")
    return tables


@dataclass(frozen=True)
class Operator:
    person: str
    comparison_group: str
    qualifications: frozenset
    timezone: str
    raw: Mapping[str, object] = field(compare=False, hash=False)

    def qualifies(self, role: str) -> bool:
        return role in self.qualifications


@dataclass(frozen=True)
class Duty:
    duty: str
    person: str
    site: str
    role: str
    start: int
    stop: int
    mandatory: bool
    raw: Mapping[str, object] = field(compare=False, hash=False)

    @property
    def span(self) -> Interval:
        return Interval(self.start, self.stop)


@dataclass(frozen=True)
class ClockRecord:
    record: str
    person: str
    site: str
    start: int
    stop: int
    state: str
    duty: Optional[str]
    tz: str
    raw: Mapping[str, object] = field(compare=False, hash=False)

    @property
    def span(self) -> Interval:
        return Interval(self.start, self.stop)


@dataclass(frozen=True)
class Loan:
    loan: str
    person: str
    original_duty: str
    new_site: str
    start: int
    stop: int
    evidence: str
    tz: str
    raw: Mapping[str, object] = field(compare=False, hash=False)

    @property
    def span(self) -> Interval:
        return Interval(self.start, self.stop)


@dataclass(frozen=True)
class BreakRecord:
    break_id: str
    person: str
    duty: Optional[str]
    start: int
    stop: int
    evidence: str
    recall_at: Optional[int]
    tz: str
    raw: Mapping[str, object] = field(compare=False, hash=False)

    @property
    def span(self) -> Interval:
        return Interval(self.start, self.stop)

    def effective_free(self) -> Optional[Interval]:
        """协议口径下实际可自由支配的休息。

        - free：整段可自由支配。
        - standby：等候指令，不算自由休息。
        - recall：从起点到 recall_at 为自由休息，被召回后即终止；
          没有 recall_at 的 recall 记录按未终止处理（整段），并在证据保留中标注。
        """
        if self.evidence == "free":
            return self.span if not self.span.is_empty else None
        if self.evidence == "recall":
            stop = self.recall_at if self.recall_at is not None else self.stop
            stop = min(stop, self.stop)
            return Interval(self.start, stop) if stop > self.start else None
        return None


@dataclass(frozen=True)
class Agreement:
    rule: str
    scope: str
    window_hours: float
    required_free_minutes: int
    minimum_coverage: float
    dedup_group: str
    compensation_minutes: int
    raw: Mapping[str, object] = field(compare=False, hash=False)

    @property
    def window_seconds(self) -> int:
        return int(round(self.window_hours * 3600))

    @property
    def required_free_seconds(self) -> int:
        return self.required_free_minutes * 60


@dataclass(frozen=True)
class NextShift:
    shift: str
    plan: str
    site: str
    role: str
    start: int
    stop: int
    candidates: tuple
    mandatory: bool
    raw: Mapping[str, object] = field(compare=False, hash=False)

    @property
    def span(self) -> Interval:
        return Interval(self.start, self.stop)


@dataclass(frozen=True)
class RestPromise:
    promise: str
    person: str
    start: int
    stop: int
    source_duty: Optional[str]
    raw: Mapping[str, object] = field(compare=False, hash=False)

    @property
    def span(self) -> Interval:
        return Interval(self.start, self.stop)


class Materials:
    """一次读入、多处复用的资料集合。"""

    def __init__(self, tables: Mapping[str, Sequence[dict]]):
        self.raw = {name: list(rows) for name, rows in tables.items()}
        self.operators: Dict[str, Operator] = {}
        for row in self.raw.get("operators", []):
            person = row["person"]
            if person in self.operators:
                raise MaterialError(f"职工编号重复：{person}")
            self.operators[person] = Operator(
                person=person,
                comparison_group=row["comparison_group"],
                qualifications=frozenset(row["qualifications"]),
                timezone=row.get("timezone") or DEFAULT_TIMEZONE,
                raw=row,
            )

        self.duties: Dict[str, Duty] = {}
        for row in self.raw.get("duties", []):
            duty = row["duty"]
            if duty in self.duties:
                raise MaterialError(f"班次编号重复：{duty}")
            tz = self.tz_for(row["person"])
            self.duties[duty] = Duty(
                duty=duty,
                person=row["person"],
                site=row["site"],
                role=row["role"],
                start=parse_instant(row["start"], tz),
                stop=parse_instant(row["stop"], tz),
                mandatory=bool(row["mandatory"]),
                raw=row,
            )

        self.clock: List[ClockRecord] = []
        for row in self.raw.get("clock", []):
            if row["state"] not in CLOCK_STATES:
                raise MaterialError(f"签到状态不认识：{row['state']}")
            tz = self.tz_for(row["person"])
            self.clock.append(
                ClockRecord(
                    record=row["record"],
                    person=row["person"],
                    site=row["site"],
                    start=parse_instant(row["start"], tz),
                    stop=parse_instant(row["stop"], tz),
                    state=row["state"],
                    duty=row.get("duty"),
                    tz=tz,
                    raw=row,
                )
            )

        self.loans: List[Loan] = []
        for row in self.raw.get("loans", []):
            if row["evidence"] not in LOAN_EVIDENCE:
                raise MaterialError(f"借调证据状态不认识：{row['evidence']}")
            tz = self.tz_for(row["person"])
            if row["original_duty"] not in self.duties:
                raise MaterialError(f"借调 {row['loan']} 指向未知原班次 {row['original_duty']}")
            self.loans.append(
                Loan(
                    loan=row["loan"],
                    person=row["person"],
                    original_duty=row["original_duty"],
                    new_site=row["new_site"],
                    start=parse_instant(row["start"], tz),
                    stop=parse_instant(row["stop"], tz),
                    evidence=row["evidence"],
                    tz=tz,
                    raw=row,
                )
            )

        self.breaks: List[BreakRecord] = []
        for row in self.raw.get("breaks", []):
            if row["evidence"] not in BREAK_EVIDENCE:
                raise MaterialError(f"休息证据状态不认识：{row['evidence']}")
            tz = self.tz_for(row["person"])
            self.breaks.append(
                BreakRecord(
                    break_id=row["break"],
                    person=row["person"],
                    duty=row.get("duty"),
                    start=parse_instant(row["start"], tz),
                    stop=parse_instant(row["stop"], tz),
                    evidence=row["evidence"],
                    recall_at=parse_optional(row.get("recall_at"), tz),
                    tz=tz,
                    raw=row,
                )
            )

        self.agreements: List[Agreement] = []
        for row in self.raw.get("agreements", []):
            self.agreements.append(
                Agreement(
                    rule=row["rule"],
                    scope=row["scope"],
                    window_hours=float(row["window_hours"]),
                    required_free_minutes=int(row["required_free_minutes"]),
                    minimum_coverage=float(row["minimum_coverage"]),
                    dedup_group=row["dedup_group"],
                    compensation_minutes=int(row["compensation_minutes"]),
                    raw=row,
                )
            )
        if not self.agreements:
            raise MaterialError("没有可用的观察协议")

        self.next_shifts: Dict[str, NextShift] = {}
        for row in self.raw.get("next_shifts", []):
            shift = row["shift"]
            if shift in self.next_shifts:
                raise MaterialError(f"候选班次编号重复：{shift}")
            tz = DEFAULT_TIMEZONE
            self.next_shifts[shift] = NextShift(
                shift=shift,
                plan=row["plan"],
                site=row["site"],
                role=row["role"],
                start=parse_instant(row["start"], tz),
                stop=parse_instant(row["stop"], tz),
                candidates=tuple(row["candidates"]),
                mandatory=bool(row["mandatory"]),
                raw=row,
            )

        self.rest_promises: Dict[str, RestPromise] = {}
        for row in self.raw.get("rest_promises", []):
            promise = row["promise"]
            if promise in self.rest_promises:
                raise MaterialError(f"补休编号重复：{promise}")
            tz = self.tz_for(row["person"])
            self.rest_promises[promise] = RestPromise(
                promise=promise,
                person=row["person"],
                start=parse_instant(row["start"], tz),
                stop=parse_instant(row["stop"], tz),
                source_duty=row.get("source_duty"),
                raw=row,
            )

        referenced = {duty.person for duty in self.duties.values()}
        referenced |= {record.person for record in self.clock}
        referenced |= {loan.person for loan in self.loans}
        referenced |= {record.person for record in self.breaks}
        for person in sorted(referenced):
            if person not in self.operators:
                raise MaterialError(f"资料引用了未登记的职工：{person}")

    # -- 查询 ---------------------------------------------------------

    def tz_for(self, person: str) -> str:
        operator = self.operators.get(person)
        return operator.timezone if operator else DEFAULT_TIMEZONE

    def operator(self, person: str) -> Optional[Operator]:
        return self.operators.get(person)

    def group_of(self, person: str) -> str:
        operator = self.operators.get(person)
        return operator.comparison_group if operator else "unknown"

    def duty_span(self, duty_id: Optional[str]) -> Optional[Interval]:
        if duty_id is None:
            return None
        duty = self.duties.get(duty_id)
        return duty.span if duty else None

    def persons(self) -> List[str]:
        seen = set(self.operators)
        for duty in self.duties.values():
            seen.add(duty.person)
        for record in self.clock:
            seen.add(record.person)
        return sorted(seen)

    def night_rule(self) -> Optional[Agreement]:
        for agreement in self.agreements:
            if agreement.scope == "night":
                return agreement
        return None

    def standard_rule(self) -> Optional[Agreement]:
        for agreement in self.agreements:
            if agreement.scope == "all":
                return agreement
        return None

    def rules_for(self, person: str) -> List[Agreement]:
        """按比较组落到适用条款：night 组同时受 all 与 night 约束。"""
        group = self.group_of(person)
        chosen: List[Agreement] = []
        for agreement in self.agreements:
            if agreement.scope in ("all", group):
                chosen.append(agreement)
        return chosen


def load(directory: Path) -> Materials:
    return Materials(read_tables(Path(directory)))
