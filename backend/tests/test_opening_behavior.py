import unittest
from datetime import datetime, timezone

from backend.research.opening_behavior import Bar, _rth_days


class OpeningBehaviorHarnessTests(unittest.TestCase):
    def test_quality_rejects_incomplete_session_and_reports_timestamp_defects(self):
        ts = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
        bars = [Bar(ts, 100, 101, 99, 100.5, 10), Bar(ts, 100.5, 101, 100, 100.75, 10)]
        days, quality = _rth_days(bars)
        self.assertEqual(days, {})
        self.assertEqual(quality["duplicate_timestamps"], 1)
        self.assertEqual(quality["non_monotonic_adjacent_timestamps"], 1)
        self.assertEqual(quality["excluded_days"], 1)

    def test_full_cash_session_is_kept(self):
        bars = []
        for minute in range(390):
            hour, rem = divmod(14 * 60 + 30 + minute, 60)
            ts = datetime(2026, 1, 5, hour, rem, tzinfo=timezone.utc)
            bars.append(Bar(ts, 100, 101, 99, 100.5, 10))
        days, quality = _rth_days(bars)
        self.assertEqual(len(days), 1)
        self.assertEqual(quality["valid_days"], 1)
        self.assertEqual(quality["invalid_ohlc_rows"], 0)


if __name__ == "__main__":
    unittest.main()
