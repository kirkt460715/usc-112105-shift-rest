"""命令行入口。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import coverage as coverage_module
from . import models
from . import plan as plan_module
from . import review as review_module
from . import state as state_module
from .timeutil import MINUTE, format_instant, hhmm


def _load(directory: Path) -> models.Materials:
    return models.load(directory)


def cmd_review(args) -> int:
    materials = _load(args.data)
    result = review_module.review(materials)
    print(review_module.render_review(materials, result))
    if args.json:
        Path(args.json).write_text(
            json.dumps(review_module.as_dict(result), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0


def cmd_coverage(args) -> int:
    materials = _load(args.data)
    result = coverage_module.compute(materials)
    print(coverage_module.render(result))
    if args.json:
        Path(args.json).write_text(
            json.dumps(coverage_module.summary(result), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0


def cmd_plans(args) -> int:
    materials = _load(args.data)
    result = review_module.review(materials)
    plans = plan_module.generate_variants(materials, result)
    results = [plan_module.evaluate(materials, result, candidate) for candidate in plans]
    if args.plan:
        chosen = [r for r in results if r.plan.name == args.plan]
        if not chosen:
            print(f"找不到方案 {args.plan}；可用：{', '.join(r.plan.name for r in results)}", file=sys.stderr)
            return 2
        print(plan_module.render_result(materials, chosen[0]))
        if args.save:
            payload = chosen[0].plan.to_dict()
            Path(args.save).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    print(plan_module.compare(results))
    if args.detail:
        for item in results:
            print()
            print(plan_module.render_result(materials, item))
    return 0


def cmd_trial(args) -> int:
    materials = _load(args.data)
    result = review_module.review(materials)
    plan = plan_module.load_plan(Path(args.plan_file).read_text(encoding="utf-8"))
    evaluated = plan_module.evaluate(materials, result, plan)
    print(plan_module.render_result(materials, evaluated))
    return 0


def cmd_confirm(args) -> int:
    materials = _load(args.data)
    result = review_module.review(materials)
    plans = plan_module.generate_variants(materials, result)
    by_name = {p.name: p for p in plans}
    if args.plan_file:
        plan = plan_module.load_plan(Path(args.plan_file).read_text(encoding="utf-8"))
    elif args.plan in by_name:
        plan = by_name[args.plan]
    else:
        print(
            f"找不到方案 {args.plan}；可用：{', '.join(by_name)}，或使用 --plan-file",
            file=sys.stderr,
        )
        return 2
    evaluated = plan_module.evaluate(materials, result, plan)
    state = state_module.build_state(materials, result, evaluated)
    from datetime import datetime, timezone

    state["confirmed_at"] = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    path = state_module.save_state(args.state, state)
    print(state_module.render_state(state))
    print()
    print(f"状态已写入 {path}")
    return 0


def cmd_status(args) -> int:
    state = state_module.load_state(args.state)
    if state is None:
        print(f"{args.state} 下没有已确认方案", file=sys.stderr)
        return 1
    print(state_module.render_state(state))
    return 0


def cmd_decisions(args) -> int:
    """解释一次具体判定：为什么某段被记为欠休或可能欠休。"""
    materials = _load(args.data)
    result = review_module.review(materials)
    lines = []
    if args.person:
        lines.append(f"{args.person} 的属地工作日与实际工时")
        timeline = result.timelines.get(args.person)
        if timeline is None:
            print(f"没有 {args.person} 的时间线", file=sys.stderr)
            return 1
        for day, day_length, certain, presumed in timeline.local_work_days():
            lines.append(
                f"  {day} 工作日长度 {hhmm(day_length)}，确定工时 {hhmm(certain)}，"
                f"按义务推定 {hhmm(presumed)}（时区 {timeline.tz}）"
            )
        stretch = timeline.longest_work_stretch()
        if stretch.duration:
            lines.append(
                f"  最长连续工作窗口 {format_instant(stretch.start, timeline.tz)}→"
                f"{format_instant(stretch.stop, timeline.tz)}（{hhmm(stretch.duration)}）"
            )
        lines.append("")
        lines.append("观察窗")
        for window in result.windows:
            if window.person != args.person:
                continue
            lines.append(
                f"  {window.duty_id} {window.rule.rule} "
                f"{format_instant(window.window.start, timeline.tz)}→"
                f"{format_instant(window.window.stop, timeline.tz)}"
                f" 休息下界/上界/推定 {window.rest_lower // MINUTE}/"
                f"{window.rest_upper // MINUTE}/{window.rest_presumed // MINUTE}"
                f" 要求 {window.required // MINUTE} 覆盖 {window.coverage:.0%}"
                f" → {review_module.STATUS_TEXT[window.status]}"
            )
            # 上界为 0 时说明原因：整段被在岗/等候证据占满，或自由休息被借调截断。
            if window.rest_upper == 0 and window.obligation:
                lines.append("      该窗内无任何可自由支配时间（在岗或等候指令覆盖整段）。")
            elif window.rest_upper < window.required:
                lines.append(
                    f"      上界也不足：窗内只剩 {window.rest_upper // MINUTE} 分钟不属于任何义务。"
                )
            for dispute in window.disputes:
                lines.append(
                    f"      争议 {format_instant(dispute.interval.start, timeline.tz)}→"
                    f"{format_instant(dispute.interval.stop, timeline.tz)}：{dispute.reason}"
                    f"（{'、'.join(dispute.sources)}）"
                )
        for note in result.person_notes.get(args.person, []):
            lines.append(f"  说明：{note}")
    else:
        for finding in result.findings:
            lines.append(
                f"{finding.finding_id} {finding.person} {review_module.STATUS_TEXT[finding.status]}"
                f" 条款 {'+'.join(finding.rule_ids)}"
                f" 休息 {finding.rest_lower // MINUTE}/{finding.rest_upper // MINUTE}"
                f" 要求 {finding.required // MINUTE}"
            )
    print("\n".join(lines))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="roster", description="联合班务核算")
    parser.add_argument("--data", type=Path, default=Path("data"), help="资料目录")
    sub = parser.add_subparsers(dest="command", required=True)

    review_parser = sub.add_parser("review", help="月度复核")
    review_parser.add_argument("--json", type=Path, help="另存机器可读结果")
    review_parser.set_defaults(func=cmd_review)

    coverage_parser = sub.add_parser("coverage", help="岗位覆盖")
    coverage_parser.add_argument("--json", type=Path, help="另存机器可读结果")
    coverage_parser.set_defaults(func=cmd_coverage)

    plans_parser = sub.add_parser("plans", help="候选班表试算")
    plans_parser.add_argument("--plan", help="只看某一方案")
    plans_parser.add_argument("--detail", action="store_true", help="打印全部方案明细")
    plans_parser.add_argument("--save", type=Path, help="把方案定义存为 JSON")
    plans_parser.set_defaults(func=cmd_plans)

    trial_parser = sub.add_parser("trial", help="试算自定义方案")
    trial_parser.add_argument("plan_file", type=Path)
    trial_parser.set_defaults(func=cmd_trial)

    confirm_parser = sub.add_parser("confirm", help="管理员确认一套方案")
    confirm_parser.add_argument("--plan", help="方案名")
    confirm_parser.add_argument("--plan-file", type=Path, help="从文件读自定义方案")
    confirm_parser.add_argument("--state", type=Path, default=Path("state"))
    confirm_parser.set_defaults(func=cmd_confirm)

    status_parser = sub.add_parser("status", help="查看已确认方案")
    status_parser.add_argument("--state", type=Path, default=Path("state"))
    status_parser.set_defaults(func=cmd_status)

    decisions_parser = sub.add_parser("decisions", help="解释判定")
    decisions_parser.add_argument("--person", help="只看某人")
    decisions_parser.set_defaults(func=cmd_decisions)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
