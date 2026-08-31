import json
import tempfile
import unittest
from pathlib import Path

from backend.research.orderflow_features import extract


class OrderflowFeatureTests(unittest.TestCase):
    def test_aggregates_book_and_trade_features(self):
        rows = [
            {"hd": {"ts_event": "1786717740000000000"}, "action": "A", "levels": [{"bid_px": "100000000000", "ask_px": "100250000000", "bid_sz": 3, "ask_sz": 1}]},
            {"hd": {"ts_event": "1786717741000000000"}, "action": "T", "side": "B", "size": 4, "levels": [{"bid_px": "100000000000", "ask_px": "100250000000", "bid_sz": 3, "ask_sz": 1}]},
            {"hd": {"ts_event": "1786717742000000000"}, "action": "T", "side": "A", "size": 1, "levels": [{"bid_px": "100000000000", "ask_px": "100250000000", "bid_sz": 3, "ask_sz": 1}]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mbp.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            result = extract(path)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["trades"], 2)
        self.assertEqual(result[0]["buy_volume"], 4)
        self.assertEqual(result[0]["sell_volume"], 1)
        self.assertAlmostEqual(result[0]["signed_trade_imbalance"], 0.6)
        self.assertGreater(result[0]["mean_book_imbalance"], 0)


if __name__ == "__main__":
    unittest.main()
