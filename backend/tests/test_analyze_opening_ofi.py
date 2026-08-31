import unittest

from backend.research.analyze_opening_ofi import cluster_bootstrap_interval, select_signals


class OpeningOfiAnalysisTests(unittest.TestCase):
    def test_thresholds_and_cooldown_are_fail_closed(self):
        second = 1_000_000_000
        rows = [
            {"bucket_start_ns": 0, "aligned_ofi": 105, "aligned_refill": 0.20},
            {"bucket_start_ns": 60 * second, "aligned_ofi": 200, "aligned_refill": 0.50},
            {"bucket_start_ns": 120 * second, "aligned_ofi": 105, "aligned_refill": 0.20},
            {"bucket_start_ns": 240 * second, "aligned_ofi": 10, "aligned_refill": 0.20},
        ]
        selected = select_signals(rows)
        self.assertEqual(
            [row["bucket_start_ns"] for row in selected],
            [0, 120 * second],
        )

    def test_cluster_bootstrap_is_deterministic(self):
        first = cluster_bootstrap_interval([1.0, 2.0, 3.0], samples=100)
        second = cluster_bootstrap_interval([1.0, 2.0, 3.0], samples=100)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
