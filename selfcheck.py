"""对资料跑一遍关键不变量，供交接前自检。

不是业务结论，而是防止口径被改坏的护栏：
- 秋季回拨按真实经过时间；
- 同日跨点只计一次个人工时；
- 已召回休息按召回时刻截断；
- 缺签到不增加“已证休息”；
- 同一合并组的工单不重复承诺；
- 候选班表不超额兑现补休。
"""
from __future__ import annotations

import sys
from pathlib import Path

from roster import coverage, models, plan, review, state
from roster.obligations import build_timelines
from roster.timeutil import MINUTE, parse_instant


def main(directory: Path) -> int:
    materials = models.load(directory)
    timelines = build_timelines(materials)
    result = review.review(materials)
    problems = []

    # 1. 回拨
    start = parse_instant("2026-11-01T01:10:00-04:00", "America/New_York")
    stop = parse_instant("2026-11-01T01:45:00-05:00", "America/New_York")
    if (stop - start) // MINUTE != 95:
        problems.append("回拨时段没有按真实经过时间计算")

    # 2. 跨点只计一次
    for person, row in coverage.compute(materials, timelines).personal.items():
        if row["counted_seconds"] > row["raw_claim_seconds"] + 1:
            problems.append(f"{person} 去重后工时大于原始申报")
        if row["deduplicated_seconds"] and row["counted_seconds"] < 0:
            problems.append(f"{person} 去重后出现负工时")

    # 3. 召回截断
    for record in materials.breaks:
        if record.evidence != "recall" or record.recall_at is None:
            continue
        effective = record.effective_free()
        if effective is None or effective.stop > record.recall_at:
            problems.append(f"{record.break_id} 召回后仍按整段休息计算")

    # 4. 未观察不计为已证休息
    for window in result.windows:
        if window.rest_lower > window.rest_upper:
            problems.append(f"{window.person} {window.duty_id} 下界高于上界")
        if window.status == review.UNDERDETERMINED and window.rest_lower >= window.required:
            problems.append(f"{window.person} {window.duty_id} 覆盖不足却被判达标")

    # 5. 合并组不重复承诺
    grouped = {}
    for finding in result.confirmed:
        key = (finding.person, finding.dedup_group)
        grouped.setdefault(key, []).append(finding)
    for key, items in grouped.items():
        items.sort(key=lambda f: f.scope.start)
        for left, right in zip(items, items[1:]):
            if left.scope.start < right.scope.stop and left.scope.stop > right.scope.start:
                problems.append(f"{key} 同时段出现多张工单")

    # 6. 候选班表不超额兑现
    for candidate in plan.generate_variants(materials, result):
        evaluated = plan.evaluate(materials, result, candidate)
        if evaluated.delivered_minutes > evaluated.owed_total:
            problems.append(f"{candidate.name} 兑现超过旧账总额")
        if evaluated.owed_remaining < 0:
            problems.append(f"{candidate.name} 剩余欠账为负")
        seen = set()
        cap = max(rule.compensation_minutes for rule in materials.agreements)
        for row in evaluated.compensation_rows:
            if row["finding"] in seen:
                problems.append(f"{candidate.name} 同一工单被重复兑现：{row['finding']}")
            seen.add(row["finding"])
            if row["minutes"] > cap and row["finding"]:
                problems.append(f"{candidate.name} 单笔补休超过条款上限：{row['finding']}")

    # 7. 已确认状态可回查
    chosen = [item for item in plan.generate_variants(materials, result) if item.name.startswith("D")]
    if chosen:
        evaluated = plan.evaluate(materials, result, chosen[0])
        snapshot = state.build_state(materials, result, evaluated)
        for delivery in snapshot["deliveries"]:
            if not delivery["original_duty"]:
                problems.append(f"{delivery['finding']} 缺少原班次引用")
        owed_ids = {item["finding"] for item in snapshot["still_owed"]}
        for delivery in snapshot["deliveries"]:
            if delivery["finding"] in owed_ids:
                problems.append(f"{delivery['finding']} 已兑现却仍挂在待安排里")

    if problems:
        for problem in problems:
            print(f"不变量未通过：{problem}", file=sys.stderr)
        return 1
    print("自检通过：回拨、去重、召回、观察覆盖、合并去重、补休额度与留痕均可复核。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "data")))
