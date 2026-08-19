import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.research.opening_ofi import (
    BookState,
    attach_forward_returns,
    book_event_components,
    extract_event_buckets,
)


PRICE_SCALE = 1_000_000_000


def row(second, *, action="A", side="B", size=1, bid=100.0, ask=100.25, bid_sz=3, ask_sz=4, instrument=1):
    return {
        "hd": {"ts_event": str(second * 1_000_000_000), "instrument_id": instrument},
        "action": action,
        "side": side,
        "size": size,
        "levels": [{
            "bid_px": str(int(bid * PRICE_SCALE)),
            "ask_px": str(int(ask * PRICE_SCALE)),
            "bid_sz": bid_sz,
            "ask_sz": ask_sz,
        }],
    }


class OpeningOfiTests(unittest.TestCase):
    def test_cont_ofi_and_queue_components_at_unchanged_prices(self):
        previous = BookState(0, 100.0, 100.25, 3, 4)
        current = BookState(1, 100.0, 100.25, 5, 2)
        result = book_event_components(previous, current)
        self.assertEqual(result["bid_queue_add"], 2)
        self.assertEqual(result["ask_queue_remove"], 2)
        self.assertEqual(result["ofi"], 4)

    def test_price_level_replacement_is_explicit(self):
        previous = BookState(0, 100.0, 100.25, 3, 4)
        current = BookState(1, 99.75, 100.50, 8, 6)
        result = book_event_components(previous, current)
        self.assertEqual(result["bid_queue_remove"], 3)
        self.assertEqual(result["ask_queue_remove"], 4)
        self.assertEqual(result["ofi"], 1)

    def test_extracts_actions_refill_and_exact_forward_labels(self):
        rows = [
            row(0, action="A", side="B", size=2, bid_sz=3, ask_sz=4),
            row(0, action="C", side="A", size=2, bid_sz=3, ask_sz=2),
            row(0, action="T", side="B", size=3, bid_sz=3, ask_sz=1),
            row(10, action="A", side="A", size=2, bid=100.25, ask=100.50, bid_sz=2, ask_sz=2),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mbp.jsonl"
            path.write_text("\n".join(json.dumps(item) for item in rows) + "\n")
            result = extract_event_buckets(path)
        self.assertEqual(len(result), 2)
        first = result[0]
        self.assertEqual(first["trades"], 1)
        self.assertEqual(first["buy_volume"], 3)
        self.assertEqual(first["cancel_ask_volume"], 2)
        self.assertGreater(first["ofi"], 0)
        attach_forward_returns(result, [10, 11])
        self.assertIsNotNone(first["forward_return_10s"])
        self.assertIsNone(first["forward_return_11s"])

    def test_invalid_horizon_is_rejected(self):
        with self.assertRaises(ValueError):
            attach_forward_returns([], [0])

    def test_dash_input_streams_without_a_temporary_raw_file(self):
        payload = json.dumps(row(0)) + "\n" + json.dumps(row(1, bid_sz=4)) + "\n"
        with patch("sys.stdin", io.StringIO(payload)):
            result = extract_event_buckets(Path("-"))
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]["ofi"], 1)

    def test_instrument_change_resets_ofi_baseline(self):
        rows = [row(0, instrument=1), row(1, instrument=2, bid=200, ask=200.25)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "roll.jsonl"
            path.write_text("\n".join(json.dumps(item) for item in rows) + "\n")
            result = extract_event_buckets(path)
        self.assertEqual(result[1]["ofi"], 0)

    def test_out_of_order_event_is_rejected(self):
        rows = [row(2), row(1)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unordered.jsonl"
            path.write_text("\n".join(json.dumps(item) for item in rows) + "\n")
            with self.assertRaises(ValueError):
                extract_event_buckets(path)


if __name__ == "__main__":
    unittest.main()
