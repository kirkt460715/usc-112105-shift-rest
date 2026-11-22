"""用最小合成资料验证数据未覆盖到的判定分支。"""
import unittest

from roster import coverage, models, review
from roster.obligations import build_timelines
from roster.timeutil import MINUTE

TZ = "America/New_York"


def materials(**overrides):
    tables = {
        "operators": [
            {"person": "A", "comparison_group": "night", "qualifications": ["access"], "timezone": TZ},
            {"person": "B", "comparison_group": "flex", "qualifications": ["access"], "timezone": TZ},
        ],
        "duties": [
            {
                "duty": "d1",
                "person": "A",
                "site": "north",
                "role": "access",
                "start": "2026-10-28T23:00:00-04:00",
                "stop": "2026-10-29T07:00:00-04:00",
                "mandatory": True,
            }
        ],
        "clock": [
            {
                "record": "c1",
                "person": "A",
                "site": "north",
                "start": "2026-10-28T23:00:00-04:00",
                "stop": "2026-10-29T07:00:00-04:00",
                "state": "onsite",
                "duty": "d1",
            }
        ],
        "loans": [],
        "breaks": [],
        "agreements": [
            {
                "rule": "standard",
                "scope": "all",
                "window_hours": 8,
                "required_free_minutes": 40,
                "minimum_coverage": 0.75,
                "dedup_group": "continuous",
                "compensation_minutes": 45,
            },
            {
                "rule": "overnight",
                "scope": "night",
                "window_hours": 7,
                "required_free_minutes": 50,
                "minimum_coverage": 0.8,
                "dedup_group": "continuous",
                "compensation_minutes": 60,
            },
        ],
        "next_shifts": [],
        "rest_promises": [],
    }
    tables.update(overrides)
    return models.Materials(tables)


class PossibleRestTests(unittest.TestCase):
    def test_short_free_break_with_loose_observation_is_possible_not_confirmed(self):
        # 签到只覆盖班次的前半段：上界足够，下界不足，覆盖达标 → 只能列为可能欠休。
        tables = materials(
            clock=[
                {
                    "record": "c1",
                    "person": "A",
                    "site": "north",
                    "start": "2026-10-28T23:00:00-04:00",
                    "stop": "2026-10-29T03:00:00-04:00",
                    "state": "onsite",
                    "duty": "d1",
                }
            ],
            breaks=[
                {
                    "break": "b1",
                    "person": "A",
                    "duty": "d1",
                    "start": "2026-10-29T02:00:00-04:00",
                    "stop": "2026-10-29T02:12:00-04:00",
                    "evidence": "free",
                    "recall_at": None,
                }
            ],
        )
        result = review.review(tables)
        statuses = {window.status for window in result.windows if window.person == "A"}
        self.assertIn(review.UNDERDETERMINED, statuses)
        for finding in result.findings:
            self.assertEqual(finding.compensation_minutes, 0)

    def test_enough_upper_bound_but_thin_lower_bound_is_possible(self):
        # 签到覆盖 84%（高于 75% 下限），上界 75 分钟够、下界只有 12 分钟：
        # 不能确认欠休，只能列为“还可能欠休”。
        tables = materials(
            clock=[
                {
                    "record": "c1",
                    "person": "A",
                    "site": "north",
                    "start": "2026-10-28T23:00:00-04:00",
                    "stop": "2026-10-29T05:45:00-04:00",
                    "state": "onsite",
                    "duty": "d1",
                }
            ],
            breaks=[
                {
                    "break": "b1",
                    "person": "A",
                    "duty": "d1",
                    "start": "2026-10-29T02:00:00-04:00",
                    "stop": "2026-10-29T02:12:00-04:00",
                    "evidence": "free",
                    "recall_at": None,
                }
            ],
        )
        result = review.review(tables)
        standard = [w for w in result.windows if w.rule.rule == "standard"][0]
        self.assertEqual(standard.status, review.POSSIBLE)
        self.assertGreaterEqual(standard.rest_upper, standard.required)
        self.assertLess(standard.rest_lower, standard.required)
        # 同一时段夜间条款的上界仍不足，合并后按更确定的结论生成一张工单，
        # 补休取最高值，而不是对同一时段重复承诺。
        finding = [f for f in result.findings if f.person == "A"][0]
        self.assertEqual(finding.status, review.CONFIRMED)
        self.assertEqual(finding.compensation_minutes, 60)
        self.assertEqual(len([f for f in result.findings if f.person == "A"]), 1)

    def test_possible_alone_never_creates_compensation(self):
        # B 属于 flex 组，只受 standard 条款约束；构造“上界够、下界不够、覆盖达标”。
        tables = materials(
            operators=[
                {"person": "B", "comparison_group": "flex", "qualifications": ["access"], "timezone": TZ},
            ],
            duties=[
                {
                    "duty": "d1",
                    "person": "B",
                    "site": "north",
                    "role": "access",
                    "start": "2026-10-28T23:00:00-04:00",
                    "stop": "2026-10-29T07:00:00-04:00",
                    "mandatory": True,
                }
            ],
            clock=[
                {
                    "record": "c1",
                    "person": "B",
                    "site": "north",
                    "start": "2026-10-28T23:00:00-04:00",
                    "stop": "2026-10-29T05:45:00-04:00",
                    "state": "onsite",
                    "duty": "d1",
                }
            ],
            breaks=[
                {
                    "break": "b1",
                    "person": "B",
                    "duty": "d1",
                    "start": "2026-10-29T02:00:00-04:00",
                    "stop": "2026-10-29T02:12:00-04:00",
                    "evidence": "free",
                    "recall_at": None,
                }
            ],
        )
        result = review.review(tables)
        self.assertEqual(len(result.windows), 1)
        self.assertEqual(result.windows[0].status, review.POSSIBLE)
        self.assertEqual(result.findings[0].compensation_minutes, 0)
        self.assertEqual(result.findings[0].status, review.POSSIBLE)
        self.assertEqual(result.confirmed, [])

    def test_partial_coverage_marks_underdetermined(self):
        tables = materials(
            clock=[
                {
                    "record": "c1",
                    "person": "A",
                    "site": "north",
                    "start": "2026-10-28T23:00:00-04:00",
                    "stop": "2026-10-29T01:00:00-04:00",
                    "state": "onsite",
                    "duty": "d1",
                }
            ]
        )
        result = review.review(tables)
        windows = [w for w in result.windows if w.person == "A"]
        self.assertTrue(windows)
        for window in windows:
            # 签到只覆盖窗内一部分；标准 8 小时窗为 2/8，夜间 7 小时窗为 2/7。
            self.assertLess(window.coverage, 0.75)
            self.assertEqual(window.status, review.UNDERDETERMINED)


class DedupTests(unittest.TestCase):
    def test_overlapping_rules_produce_one_finding_at_highest_compensation(self):
        tables = materials()
        result = review.review(tables)
        findings = [f for f in result.findings if f.person == "A"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].compensation_minutes, 60)
        self.assertEqual(findings[0].rule_ids, ["overnight", "standard"])

    def test_recalled_break_does_not_restore_approval(self):
        tables = materials(
            breaks=[
                {
                    "break": "b1",
                    "person": "A",
                    "duty": "d1",
                    "start": "2026-10-29T02:00:00-04:00",
                    "stop": "2026-10-29T02:50:00-04:00",
                    "evidence": "recall",
                    "recall_at": "2026-10-29T02:12:00-04:00",
                }
            ]
        )
        result = review.review(tables)
        timeline = result.timelines["A"]
        free = [(p.interval.start, p.interval.stop) for p in timeline.free]
        self.assertEqual(free, [(free[0][0], free[0][0] + 12 * MINUTE)])
        for window in result.windows:
            if window.person == "A":
                self.assertEqual(window.rest_lower, 12 * MINUTE)
                self.assertEqual(window.status, review.CONFIRMED)


class CoverageSyntheticTests(unittest.TestCase):
    def test_confirmed_loan_removes_original_site_coverage(self):
        tables = materials(
            operators=[
                {"person": "A", "comparison_group": "night", "qualifications": ["access"], "timezone": TZ},
                {"person": "B", "comparison_group": "flex", "qualifications": ["access"], "timezone": TZ},
            ],
            clock=[
                {
                    "record": "c1",
                    "person": "A",
                    "site": "north",
                    "start": "2026-10-28T23:00:00-04:00",
                    "stop": "2026-10-29T07:00:00-04:00",
                    "state": "onsite",
                    "duty": "d1",
                }
            ],
            loans=[
                {
                    "loan": "x1",
                    "person": "A",
                    "original_duty": "d1",
                    "new_site": "port",
                    "start": "2026-10-29T03:00:00-04:00",
                    "stop": "2026-10-29T05:00:00-04:00",
                    "evidence": "confirmed",
                }
            ],
        )
        timelines = build_timelines(tables)
        report = coverage.compute(tables, timelines)
        north = report.duties["d1"]
        # 借调生效后原岗在该两小时内不再计为覆盖，但其余时段仍有签到。
        uncovered_minutes = sum(piece.duration for piece in north.uncovered) // MINUTE
        self.assertEqual(uncovered_minutes, 120)
        row = report.personal["A"]
        self.assertGreater(row["deduplicated_seconds"], 0)

    def test_contested_loan_puts_both_sites_in_doubt(self):
        tables = materials(
            clock=[
                {
                    "record": "c1",
                    "person": "A",
                    "site": "north",
                    "start": "2026-10-28T23:00:00-04:00",
                    "stop": "2026-10-29T07:00:00-04:00",
                    "state": "onsite",
                    "duty": "d1",
                }
            ],
            loans=[
                {
                    "loan": "x1",
                    "person": "A",
                    "original_duty": "d1",
                    "new_site": "port",
                    "start": "2026-10-29T03:00:00-04:00",
                    "stop": "2026-10-29T05:00:00-04:00",
                    "evidence": "conflict",
                }
            ],
        )
        report = coverage.compute(tables, build_timelines(tables))
        contested = sum(piece.duration for piece in report.duties["d1"].contested) // MINUTE
        self.assertEqual(contested, 120)


if __name__ == "__main__":
    unittest.main()
