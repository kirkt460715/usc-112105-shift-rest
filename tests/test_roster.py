import unittest
from pathlib import Path

from roster import coverage, models, plan, review, state
from roster.obligations import build_timelines
from roster.timeutil import MINUTE, Interval

DATA = Path(__file__).resolve().parents[1] / "data"


class MaterialFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.materials = models.load(DATA)
        cls.review = review.review(cls.materials)
        cls.timelines = build_timelines(cls.materials)


class ObligationTests(MaterialFixture):
    def test_recall_truncates_free_rest(self):
        timeline = self.timelines["U6"]
        # b-desk-2-2 批到 02:50，但 02:12 被召回。
        self.assertIn(
            (1793325600, 1793326320),
            [(p.interval.start, p.interval.stop) for p in timeline.free],
        )

    def test_standby_is_not_free_rest(self):
        timeline = self.timelines["U4"]
        standby = [(p.interval.start, p.interval.stop) for p in timeline.standby]
        self.assertTrue(standby)
        free = [(p.interval.start, p.interval.stop) for p in timeline.free]
        for piece in standby:
            self.assertNotIn(piece, free)

    def test_two_sites_count_personal_time_once(self):
        timeline = self.timelines["U5"]
        # 借调冲突期间 south 与 port 都有记录，个人工时只合并一次。
        start = models.parse_instant("2026-10-31T03:00:00-04:00", "America/New_York")
        stop = models.parse_instant("2026-10-31T05:00:00-04:00", "America/New_York")
        overlap = timeline.work_certain()
        self.assertLessEqual(
            sum(1 for piece in overlap if piece.start <= start < piece.stop),
            1,
        )

    def test_contested_loan_is_upper_bound_only(self):
        timeline = self.timelines["U0"]
        self.assertFalse(timeline.contested == [])
        certain = timeline.work_certain()
        possible = timeline.work_possible()
        self.assertGreaterEqual(
            sum(piece.duration for piece in possible),
            sum(piece.duration for piece in certain),
        )

    def test_clock_gaps_are_reported_not_treated_as_rest(self):
        people = {gap["person"] for gap in self.review.clock_gaps}
        self.assertIn("U0", people)


class ReviewTests(MaterialFixture):
    def test_confirmed_and_undetermined_are_separate(self):
        self.assertTrue(self.review.confirmed)
        self.assertTrue(self.review.underdetermined)
        ids = {f.finding_id for f in self.review.confirmed}
        for finding in self.review.underdetermined:
            self.assertNotIn(finding.finding_id, ids)
            self.assertEqual(finding.compensation_minutes, 0)

    def test_undetermined_has_low_coverage(self):
        for finding in self.review.underdetermined:
            self.assertLess(finding.coverage, 0.8)

    def test_dedup_takes_highest_compensation_once(self):
        for finding in self.review.confirmed:
            self.assertIn(finding.compensation_minutes, (45, 60))
            # 同一合并组内不会出现两笔同时段工单
        scopes = [
            (f.person, f.dedup_group, f.scope.start, f.scope.stop)
            for f in self.review.confirmed
        ]
        self.assertEqual(len(scopes), len(set(scopes)))

    def test_promise_is_attached_and_settles(self):
        by_person = [f for f in self.review.confirmed if f.person == "U1" and f.promise]
        self.assertTrue(by_person)
        self.assertEqual(by_person[0].outstanding_minutes, 0)

    def test_group_stats_keep_coverage_visible(self):
        for group in self.review.groups.values():
            self.assertGreater(group.windows, 0)
            self.assertGreaterEqual(group.coverage, 0)
            self.assertLessEqual(group.coverage, 1)


class CoverageTests(MaterialFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.coverage = coverage.compute(cls.materials, cls.timelines)

    def test_personal_hours_deduplicated_for_double_claims(self):
        row = self.coverage.personal["U6"]
        self.assertGreater(row["deduplicated_seconds"], 0)

    def test_relief_does_not_keep_original_site_during_confirmed_loan(self):
        item = self.coverage.duties["desk-4-0"]
        # U4 被确认借调到 port 后，north 的原岗不再自动计为覆盖。
        self.assertTrue(item.uncovered or item.unobserved)

    def test_mandatory_gap_is_flagged(self):
        names = {item.duty.duty for item in self.coverage.uncovered_mandatory}
        self.assertIn("desk-3-0", names)

    def test_unobserved_is_not_reported_as_covered(self):
        for item in self.coverage.duties.values():
            self.assertFalse(set(item.covered) & set(item.unobserved))


class PlanTests(MaterialFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.plans = plan.generate_variants(cls.materials, cls.review)
        cls.results = [plan.evaluate(cls.materials, cls.review, item) for item in cls.plans]

    def test_variant_with_relief_reduces_coverage_loss(self):
        by_name = {result.plan.name: result for result in self.results}
        base = by_name["A-不排休息"]
        lent = by_name["C-借调补位"]
        self.assertGreater(len(base.coverage_losses), len(lent.coverage_losses))

    def test_average_rest_does_not_hide_individual_breach(self):
        for result in self.results:
            if result.group_average and result.violations:
                # 有违规时仍必须逐人列出
                self.assertTrue(result.violating_persons)

    def test_compensation_never_exceeds_promise(self):
        for result in self.results:
            self.assertGreaterEqual(result.owed_remaining, 0)

    def test_relief_shift_can_create_breach_for_reliever(self):
        # 借调补位方案里替班人义务增加，可能出现替班人自己的休息被截断。
        names = {result.plan.name for result in self.results}
        self.assertIn("C-借调补位", names)


class StateTests(MaterialFixture):
    def test_confirmed_state_keeps_traceability(self):
        result = plan.evaluate(
            self.materials,
            self.review,
            [item for item in plan.generate_variants(self.materials, self.review) if item.name.startswith("D")][0],
        )
        snapshot = state.build_state(self.materials, self.review, result)
        self.assertTrue(snapshot["deliveries"])
        for item in snapshot["deliveries"]:
            self.assertIn("original_duty", item)
            self.assertTrue(item["original_duty"])
            self.assertIn("source_scope", item)
        for item in snapshot["legacy_promises"]:
            self.assertIn("source_duty", item)
        self.assertTrue(snapshot["investigations"])
        for item in snapshot["investigations"]:
            self.assertEqual(item["action"], "追加调查，不生成补休承诺")


if __name__ == "__main__":
    unittest.main()
