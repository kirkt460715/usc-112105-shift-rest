"""管理员确认与状态留痕。

确认一套候选班表后写入状态目录：
- 兑现时段：哪些旧欠休被安排、由谁替班、兑现多少分钟；
- 仍待调查：观察不足与证据冲突的工单，只挂调查、不产生补休承诺；
- 旧承诺：每张工单的原班表、原条款、原补休时长与签收记录都保留可查；
- 未安排的旧欠账：已确认但本方案未兑现的部分继续挂账。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from . import plan as plan_module
from .models import Materials
from .review import Review
from .timeutil import format_instant

STATE_FILE = "confirmed_plan.json"


def _scope(finding, tz: str) -> dict:
    return {
        "start": format_instant(finding.scope.start, tz),
        "stop": format_instant(finding.scope.stop, tz),
    }


def build_state(materials: Materials, review: Review, result: plan_module.PlanResult, tz: str = "America/New_York") -> dict:
    """把一次确认整理成可回查的结构。"""
    delivered = {row["finding"]: row for row in result.compensation_rows if row["deliverable"]}
    promised = {finding.finding_id: finding for finding in review.confirmed}

    deliveries: List[dict] = []
    for finding in review.confirmed:
        row = delivered.get(finding.finding_id)
        if row is None:
            continue
        deliveries.append(
            {
                "finding": finding.finding_id,
                "person": finding.person,
                "rule": finding.rule_ids,
                "original_duty": finding.duties,
                "promised_minutes": finding.compensation_minutes,
                "delivered_minutes": row["minutes"],
                "window_start": format_instant(row["start"], tz),
                "window_stop": format_instant(row["stop"], tz),
                "relief": row["relief"],
                "source_promise": finding.promise,
                "source_scope": _scope(finding, tz),
                "source_review": {
                    "rest_lower_minutes": finding.rest_lower // 60,
                    "rest_upper_minutes": finding.rest_upper // 60,
                    "required_minutes": finding.required // 60,
                    "coverage": round(finding.coverage, 4),
                    "disputes": [
                        {
                            "start": format_instant(dispute.interval.start, tz),
                            "stop": format_instant(dispute.interval.stop, tz),
                            "reason": dispute.reason,
                            "sources": list(dispute.sources),
                        }
                        for dispute in finding.disputes
                    ],
                },
            }
        )

    still_owed: List[dict] = []
    for finding in review.confirmed:
        row = delivered.get(finding.finding_id)
        already = finding.scheduled_minutes
        done = already + (row["minutes"] if row else 0)
        remaining = max(0, finding.compensation_minutes - done)
        if not remaining:
            continue
        still_owed.append(
            {
                "finding": finding.finding_id,
                "person": finding.person,
                "rule": finding.rule_ids,
                "owed_minutes": finding.compensation_minutes,
                "settled_minutes": min(finding.compensation_minutes, done),
                "remaining_minutes": remaining,
                "state": "已签收待兑现" if finding.promise else "未安排",
                "source_promise": finding.promise,
                "source_scope": _scope(finding, tz),
            }
        )

    investigations: List[dict] = []
    for finding in list(review.possible) + list(review.underdetermined):
        investigations.append(
            {
                "finding": finding.finding_id,
                "person": finding.person,
                "status": finding.status,
                "rule": finding.rule_ids,
                "coverage": round(finding.coverage, 4),
                "rest_lower_minutes": finding.rest_lower // 60,
                "rest_upper_minutes": finding.rest_upper // 60,
                "required_minutes": finding.required // 60,
                "scope": _scope(finding, tz),
                "sources": sorted({s for d in finding.disputes for s in d.sources}),
                "action": "追加调查，不生成补休承诺",
            }
        )

    state = {
        "plan": result.plan.name,
        "plan_definition": result.plan.to_dict(),
        "confirmed_at": None,
        "summary": {
            "delivered_minutes": result.delivered_minutes,
            "remaining_minutes": result.owed_remaining,
            "mandatory_coverage_losses": [
                {
                    "shift": item.shift.shift,
                    "site": item.shift.site,
                    "role": item.shift.role,
                    "reason": item.reason,
                }
                for item in result.coverage_losses
            ],
            "rest_violations": [
                {
                    "person": item.person,
                    "shift": item.shift,
                    "rule": item.rule,
                    "achieved_minutes": item.achieved // 60,
                    "required_minutes": item.required // 60,
                }
                for item in result.violations
            ],
        },
        "deliveries": deliveries,
        "still_owed": still_owed,
        "investigations": investigations,
        "legacy_promises": [
            {
                "promise": promise.promise,
                "person": promise.person,
                "start": format_instant(promise.start, tz),
                "stop": format_instant(promise.stop, tz),
                "source_duty": promise.source_duty,
                "source_duty_start": (
                    format_instant(materials.duties[promise.source_duty].start, tz)
                    if promise.source_duty in materials.duties
                    else None
                ),
                "settled_findings": sorted(
                    finding.finding_id
                    for finding in promised.values()
                    if finding.promise == promise.promise
                ),
                "standing": (
                    "对应工单未出现在本期已确认欠休中"
                    if promise.source_duty
                    and not any(
                        finding.promise == promise.promise for finding in promised.values()
                    )
                    else "已对应本期工单"
                ),
                "delivered_this_plan": sorted(
                    row["finding"]
                    for row in deliveries
                    if row["source_promise"] == promise.promise
                ),
            }
            for promise in materials.rest_promises.values()
        ],
        "carried_from_plan": None,
    }
    return state


def save_state(directory: Path, state: dict) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / STATE_FILE
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        state["carried_from_plan"] = {
            "plan": previous.get("plan"),
            "still_owed": previous.get("still_owed", []),
        }
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_state(directory: Path) -> Optional[dict]:
    path = Path(directory) / STATE_FILE
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def render_state(state: dict) -> str:
    lines: List[str] = []
    lines.append(f"已确认方案：{state['plan']}")
    lines.append("=" * 8)
    summary = state["summary"]
    lines.append(
        f"兑现 {summary['delivered_minutes']} 分钟；仍欠 {summary['remaining_minutes']} 分钟；"
        f"必守岗位缺口 {len(summary['mandatory_coverage_losses'])} 个；"
        f"个体休息不足 {len(summary['rest_violations'])} 处。"
    )

    lines.append("")
    lines.append("一、本次兑现时段")
    if not state["deliveries"]:
        lines.append("  无。")
    for item in state["deliveries"]:
        lines.append(
            f"  {item['finding']} {item['person']} {item['window_start']}→{item['window_stop']}"
            f" 兑现 {item['delivered_minutes']}/{item['promised_minutes']} 分钟"
            f"（条款 {'+'.join(item['rule'])}，原班次 {'、'.join(item['original_duty'])}）"
            + (f"，替班 {item['relief']}" if item["relief"] else "")
            + (f"，源自已签收 {item['source_promise']}" if item["source_promise"] else "")
        )

    lines.append("")
    lines.append("二、仍未安排的旧欠账")
    if not state["still_owed"]:
        lines.append("  无。")
    for item in state["still_owed"]:
        lines.append(
            f"  {item['finding']} {item['person']} {item['state']}"
            f" 尚欠 {item['remaining_minutes']} 分钟"
            f"（原范围 {item['source_scope']['start']}→{item['source_scope']['stop']}）"
            + (f"，已签收 {item['source_promise']}" if item["source_promise"] else "")
        )

    lines.append("")
    lines.append("三、仍待调查（不生成补休承诺）")
    if not state["investigations"]:
        lines.append("  无。")
    for item in state["investigations"]:
        lines.append(
            f"  {item['finding']} {item['person']} {item['status']}"
            f" 覆盖 {item['coverage']:.0%}，休息 {item['rest_lower_minutes']}–{item['rest_upper_minutes']} 分钟"
            f" / 要求 {item['required_minutes']} 分钟；{item['action']}"
            + (f"；证据 {'、'.join(item['sources'])}" if item["sources"] else "")
        )

    lines.append("")
    lines.append("四、旧班表承诺可回查")
    if not state["legacy_promises"]:
        lines.append("  无。")
    for item in state["legacy_promises"]:
        text = (
            f"  {item['promise']} {item['person']} {item['start']}→{item['stop']}"
            f" 原班次 {item['source_duty'] or '—'}"
        )
        if item.get("source_duty_start"):
            text += f"（原班次开始 {item['source_duty_start']}）"
        if item["settled_findings"]:
            text += f"；对应工单 {'、'.join(item['settled_findings'])}"
        elif item.get("standing"):
            text += f"；{item['standing']}（原班次本期无需补休，承诺仍按已签收保留）"
        if item.get("delivered_this_plan"):
            text += f"；本期兑现 {'、'.join(item['delivered_this_plan'])}"
        lines.append(text)

    if state.get("carried_from_plan"):
        carried = state["carried_from_plan"]
        lines.append("")
        lines.append("五、与上一版确认的衔接")
        lines.append(f"  上一版方案：{carried['plan']}")
        for item in carried["still_owed"]:
            lines.append(
                f"  承接 {item['finding']} {item['person']} 尚欠 {item['remaining_minutes']} 分钟"
            )
    return "\n".join(lines)
