import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from roster.timeutil import (
    Interval,
    TimeError,
    clip,
    local_day,
    longest,
    merge_within,
    normalize,
    parse_instant,
    subtract,
    total,
)


TZ = "America/New_York"


class InstantTests(unittest.TestCase):
    def test_fall_back_uses_elapsed_time_not_wall_clock(self):
        start = parse_instant("2026-11-01T01:10:00-04:00", TZ)
        stop = parse_instant("2026-11-01T01:45:00-05:00", TZ)
        # 墙上时间只差 35 分钟，真实经过 95 分钟。
        self.assertEqual((stop - start) // 60, 95)

    def test_half_hour_after_fold(self):
        folded = parse_instant("2026-11-01T01:45:00-05:00", TZ)
        next_moment = parse_instant("2026-11-01T02:10:00-05:00", TZ)
        self.assertEqual((next_moment - folded) // 60, 25)

    def test_naive_time_is_read_in_local_zone(self):
        naive = parse_instant("2026-11-01T12:00:00", TZ)
        aware = parse_instant("2026-11-01T12:00:00-05:00", TZ)
        self.assertEqual(naive, aware)

    def test_ambiguous_naive_fold_takes_first_occurrence(self):
        # 回拨当日的 01:10 出现两次；不带偏移量时取第一次（-04:00），
        # 因而与明确写 -05:00 的记录不是同一瞬时。
        naive = parse_instant("2026-11-01T01:10:00", TZ)
        self.assertEqual(naive, parse_instant("2026-11-01T01:10:00-04:00", TZ))
        self.assertNotEqual(naive, parse_instant("2026-11-01T01:10:00-05:00", TZ))

    def test_rejects_unparsable(self):
        with self.assertRaises(TimeError):
            parse_instant("not-a-time", TZ)

    def test_local_day_uses_site_date(self):
        instant = parse_instant("2026-11-01T03:30:00+00:00", TZ)
        self.assertEqual(local_day(instant, TZ), "2026-10-31")

    def test_fold_day_is_25_hours(self):
        start = parse_instant("2026-11-01T00:00:00-04:00", TZ)
        stop = parse_instant("2026-11-02T00:00:00-05:00", TZ)
        self.assertEqual((stop - start) // 3600, 25)


class IntervalTests(unittest.TestCase):
    def test_half_open_overlap(self):
        self.assertFalse(Interval(0, 10).overlaps(Interval(10, 20)))
        self.assertTrue(Interval(0, 11).overlaps(Interval(10, 20)))

    def test_normalize_merges_touching(self):
        merged = normalize([Interval(0, 5), Interval(5, 10)])
        self.assertEqual(merged, [Interval(0, 10)])

    def test_subtract_keeps_tails(self):
        pieces = subtract([Interval(0, 20)], [Interval(5, 10)])
        self.assertEqual(pieces, [Interval(0, 5), Interval(10, 20)])

    def test_clip_and_total(self):
        pieces = clip([Interval(0, 20)], 5, 15)
        self.assertEqual(total(pieces), 10)

    def test_merge_within_tolerates_short_gaps(self):
        merged = merge_within([Interval(0, 10), Interval(12, 20)], gap=2)
        self.assertEqual(merged, [Interval(0, 20)])

    def test_longest(self):
        self.assertEqual(longest([Interval(0, 5), Interval(10, 30)]), 20)


if __name__ == "__main__":
    unittest.main()
