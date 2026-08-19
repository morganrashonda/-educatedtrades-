import json
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.research.opening_nq_qqq_bridge import EquityQuote
from backend.research.opening_nq_qqq_forward import (
    AlpacaQQQProvider,
    BudgetRefusal,
    CONTRACT_SHA256,
    DatabentoMinuteProvider,
    ET,
    FIRST_ELIGIBLE_SESSION,
    ForwardRefusal,
    ForwardStore,
    MAX_ORDERFLOW_BYTES,
    MIN_FREE_DISK_HEADROOM_BYTES,
    NQBar,
    QQQ_PRE_ENTRY_HMS,
    THRESHOLD_PCT,
    _base_payload,
    gap_prediction_features,
    observe_session,
    qqq_pre_entry_features,
    summarize_orderflow,
)


DAY = date(2026, 8, 19)
PRIOR = date(2026, 8, 18)
AFTER_CLOSE = datetime(2026, 8, 19, 16, 30, tzinfo=ET)


def at(day, hour, minute, second=0, microsecond=0):
    return datetime(day.year, day.month, day.day, hour, minute, second, microsecond, tzinfo=ET)


def bar(day, hour, minute, close, instrument_id=42):
    text = f"{day}|{hour}:{minute}|{close}|{instrument_id}"
    return NQBar(at(day, hour, minute), close, instrument_id, text)


def quote(day, hour, minute, second, bid, ask, microsecond=1):
    ts = at(day, hour, minute, second, microsecond)
    return EquityQuote(
        ts, bid, ask, 10.0, 11.0, "Q", "P",
        ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


def qqq_marks(day=DAY, short=False):
    if short:
        prices = {
            "entry": (100.00, 100.02), "delayed_5": (99.96, 99.98),
            "delayed_10": (99.91, 99.93), "exit": (99.80, 99.82),
        }
    else:
        prices = {
            "entry": (100.00, 100.02), "delayed_5": (100.04, 100.06),
            "delayed_10": (100.09, 100.11), "exit": (100.20, 100.22),
        }
    times = {
        "entry": (9, 30, 1), "delayed_5": (9, 30, 6),
        "delayed_10": (9, 30, 11), "exit": (9, 32, 1),
    }
    return {name: quote(day, *times[name], *prices[name]) for name in times}


def qqq_pre_entry_quote(day=DAY):
    return quote(day, *QQQ_PRE_ENTRY_HMS, 99.98, 100.00)


class NQProvider:
    def __init__(
        self, prior_close=100.0, current_close=98.0, prior_id=42, current_id=42,
        orderflow_result=None, orderflow_error=None,
    ):
        self.prior = bar(PRIOR, 15, 59, prior_close, prior_id)
        self.current = bar(DAY, 9, 28, current_close, current_id)
        self.calls = []
        self.orderflow_result = orderflow_result or {
            "status": "COMPLETE", "diagnostic_only": True,
            "may_affect_signal_or_primary_outcome": False, "windows": {},
        }
        self.orderflow_error = orderflow_error

    def fetch_bar(self, day, hms):
        self.calls.append(("current", day, hms))
        return self.current

    def prior_close(self, day, prior_day, prior_close_time):
        self.calls.append(("prior", day, prior_day, prior_close_time))
        assert prior_day == PRIOR
        return PRIOR, self.prior

    def collect_orderflow(self, day, instrument_id, direction, raw_dir):
        self.calls.append(("orderflow", day, instrument_id, direction, raw_dir))
        if self.orderflow_error:
            raise self.orderflow_error
        return self.orderflow_result


class QQQProvider:
    def __init__(
        self, marks=None, prior_close_time=time(16, 0), pre_entry=None,
        pre_entry_error=None,
    ):
        self.value = marks if marks is not None else qqq_marks()
        self.calls = 0
        self.prior_close_time = prior_close_time
        self.pre_entry_value = (
            pre_entry if pre_entry is not None else qqq_pre_entry_quote()
        )
        self.pre_entry_error = pre_entry_error
        self.pre_entry_calls = 0

    def session_context(self, day):
        from backend.research.opening_nq_qqq_forward import CashSessionContext
        return CashSessionContext(day, PRIOR, self.prior_close_time)

    def marks(self, day):
        self.calls += 1
        return self.value

    def pre_entry_mark(self, day):
        self.pre_entry_calls += 1
        if self.pre_entry_error:
            raise self.pre_entry_error
        return self.pre_entry_value


class ExplodingProvider:
    calls = 0

    def fetch_bar(self, *_args):
        self.calls += 1
        raise AssertionError("provider must not be called")

    def session_context(self, *_args):
        self.calls += 1
        raise AssertionError("provider must not be called")


def make_store(tmp_path):
    return ForwardStore(tmp_path / "forward.sqlite3")


def test_pre_freeze_and_same_day_too_early_refuse_before_network(tmp_path):
    store = make_store(tmp_path)
    provider = ExplodingProvider()
    with pytest.raises(ForwardRefusal, match="predates"):
        observe_session(date(2026, 8, 18), store, provider, provider, AFTER_CLOSE)
    with pytest.raises(ForwardRefusal, match="16:20"):
        observe_session(DAY, store, provider, provider, at(DAY, 16, 19, 59))
    assert provider.calls == 0
    assert store.events(DAY) == []


def test_weekend_and_future_sessions_refuse(tmp_path):
    store = make_store(tmp_path)
    provider = ExplodingProvider()
    with pytest.raises(ForwardRefusal, match="weekend"):
        observe_session(date(2026, 8, 22), store, provider, provider, at(date(2026, 8, 22), 17, 0))
    with pytest.raises(ForwardRefusal, match="future"):
        observe_session(date(2026, 8, 20), store, provider, provider, AFTER_CLOSE)


def test_prior_early_close_refuses_instead_of_skipping_to_older_bar(tmp_path):
    store = make_store(tmp_path)
    result = observe_session(
        DAY, store, NQProvider(), QQQProvider(prior_close_time=time(13, 0)), AFTER_CLOSE
    )
    assert result["status"] == "REFUSED_NQ_SOURCE"
    assert "closed early" in result["reason"]


class CalendarResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload

    def close(self):
        return None


class CalendarClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, headers, params, timeout):
        self.calls.append((url, headers, params, timeout))
        return CalendarResponse(self.payload)


def test_alpaca_calendar_selects_immediately_preceding_actual_session():
    client = CalendarClient([
        {"date": "2026-08-17", "open": "09:30", "close": "16:00"},
        {"date": "2026-08-18", "open": "09:30", "close": "13:00"},
        {"date": "2026-08-19", "open": "09:30", "close": "16:00"},
    ])
    provider = AlpacaQQQProvider("key", "secret", client=client)
    context = provider.session_context(DAY)
    assert context.prior_session_date == PRIOR
    assert context.prior_close_time == time(13, 0)
    assert client.calls[0][0] == "https://paper-api.alpaca.markets/v2/calendar"
    assert client.calls[0][2] == {"start": "2026-08-05", "end": "2026-08-19"}


def test_alpaca_calendar_refuses_non_session_day():
    client = CalendarClient([
        {"date": "2026-08-18", "open": "09:30", "close": "16:00"},
    ])
    provider = AlpacaQQQProvider("key", "secret", client=client)
    with pytest.raises(Exception, match="not an Alpaca cash-market session"):
        provider.session_context(DAY)


def test_calendar_outage_is_distinct_and_retryable(tmp_path):
    store = make_store(tmp_path)

    class CalendarDown(QQQProvider):
        def session_context(self, _day):
            raise RuntimeError("calendar unavailable")

    result = observe_session(DAY, store, ExplodingProvider(), CalendarDown(), AFTER_CLOSE)
    assert result["status"] == "REFUSED_CALENDAR_SOURCE"
    assert "calendar unavailable" in result["reason"]


class DBResponse:
    def __init__(self, payload=None, text=""):
        self.payload = payload
        self.text = text

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload

    def close(self):
        return None


class DBClient:
    def __init__(self, cost=0.001):
        self.cost = cost
        self.calls = []

    def get(self, url, params, auth, timeout):
        self.calls.append((url, dict(params), auth, timeout))
        if url.endswith("metadata.get_cost"):
            return DBResponse(self.cost)
        ts = int(at(DAY, 9, 28).astimezone(timezone.utc).timestamp() * 1e9)
        row = {
            "hd": {"ts_event": str(ts), "instrument_id": 42},
            "close": "98000000000",
        }
        return DBResponse(text=json.dumps(row) + "\n")


def test_databento_request_is_one_exact_minute_and_decodes_raw_price():
    client = DBClient()
    provider = DatabentoMinuteProvider("private-key", client=client)
    result = provider.fetch_bar(DAY, (9, 28, 0))
    assert result.close == pytest.approx(98.0)
    assert result.instrument_id == 42
    assert provider.estimated_cost_usd == pytest.approx(0.001)
    assert provider.request_count == 1
    assert len(client.calls) == 2
    data_params = client.calls[1][1]
    assert data_params["start"] == "2026-08-19T13:28:00Z"
    assert data_params["end"] == "2026-08-19T13:29:00Z"
    assert data_params["schema"] == "ohlcv-1m"
    assert data_params["symbols"] == "NQ.v.0"
    assert "private-key" not in json.dumps(data_params)


def test_databento_cost_and_request_caps_refuse_before_data_request():
    expensive = DBClient(cost=0.011)
    provider = DatabentoMinuteProvider("key", client=expensive)
    with pytest.raises(BudgetRefusal, match=r"\$0.01"):
        provider.fetch_bar(DAY, (9, 28, 0))
    assert len(expensive.calls) == 1
    assert provider.request_count == 0

    no_requests = DBClient()
    provider = DatabentoMinuteProvider("key", client=no_requests, max_requests=0)
    with pytest.raises(BudgetRefusal, match="request-count"):
        provider.fetch_bar(DAY, (9, 28, 0))
    assert no_requests.calls == []


class OrderflowStreamResponse:
    def __init__(self, body, declared=None):
        self.body = body
        self.headers = {} if declared is None else {"Content-Length": str(declared)}

    def raise_for_status(self):
        return None

    def iter_content(self, _size):
        midpoint = len(self.body) // 2
        yield self.body[:midpoint]
        yield self.body[midpoint:]

    def close(self):
        return None


class OrderflowClient:
    def __init__(self, cost=0.10, instrument_id=42, declared=None):
        self.cost = cost
        self.instrument_id = instrument_id
        self.declared = declared
        self.calls = []

    def get(self, url, params, auth, timeout, stream=False):
        self.calls.append((url, dict(params), auth, timeout, stream))
        if url.endswith("metadata.get_cost"):
            return DBResponse(self.cost)
        start = at(DAY, 9, 29, 55).astimezone(timezone.utc)
        lines = []
        for second in range(131):
            ts_ns = int((start + timedelta(seconds=second)).timestamp() * 1e9)
            bid = 100.0 + second * 0.01
            row = {
                "hd": {"ts_event": str(ts_ns), "instrument_id": self.instrument_id},
                "action": "T", "side": "B" if second % 2 == 0 else "A", "size": 2,
                "levels": [{
                    "bid_px": str(int(bid * 1_000_000_000)),
                    "ask_px": str(int((bid + 0.25) * 1_000_000_000)),
                    "bid_sz": 10, "ask_sz": 9,
                }],
            }
            lines.append(json.dumps(row))
        body = ("\n".join(lines) + "\n").encode()
        return OrderflowStreamResponse(body, self.declared)


def test_orderflow_download_is_capped_validated_compressed_and_reusable(tmp_path):
    client = OrderflowClient()
    provider = DatabentoMinuteProvider("private-key", client=client)
    result = provider.collect_orderflow(DAY, 42, -1, tmp_path)
    assert result["status"] == "COMPLETE"
    assert result["diagnostic_only"] is True
    assert result["may_affect_signal_or_primary_outcome"] is False
    assert set(result["windows"]) == {
        "pre_entry_6_seconds", "first_10_seconds", "first_30_seconds",
        "primary_120_seconds",
    }
    pre_entry = result["pre_entry_prediction_inputs"]
    assert pre_entry == result["windows"]["pre_entry_6_seconds"]
    assert pre_entry["covered_seconds"] == 6
    assert pre_entry["available_before_entry"] is True
    assert pre_entry["contains_post_entry_information"] is False
    provenance = result["provenance"]
    assert provenance["records"] == 131
    assert provenance["estimated_new_cost_usd"] == pytest.approx(0.10)
    assert provenance["compressed_bytes"] > 0
    target = tmp_path / f"nq_mbp1_{DAY}_42.jsonl.gz"
    assert target.exists()
    assert not list(tmp_path.glob("*.part"))
    calls = len(client.calls)
    reused = provider.collect_orderflow(DAY, 42, -1, tmp_path)
    assert reused["provenance"]["reused"] is True
    assert reused["provenance"]["estimated_new_cost_usd"] == 0
    assert len(client.calls) == calls


def test_orderflow_cost_size_and_instrument_fail_closed(tmp_path):
    expensive = OrderflowClient(cost=0.51)
    provider = DatabentoMinuteProvider("key", client=expensive)
    with pytest.raises(BudgetRefusal, match=r"\$0.50"):
        provider.collect_orderflow(DAY, 42, 1, tmp_path / "cost")
    assert len(expensive.calls) == 1

    oversized = OrderflowClient(declared=MAX_ORDERFLOW_BYTES + 1)
    provider = DatabentoMinuteProvider("key", client=oversized)
    with pytest.raises(BudgetRefusal, match="64 MiB"):
        provider.collect_orderflow(DAY, 42, 1, tmp_path / "size")
    assert not list((tmp_path / "size").glob("*.gz"))

    wrong = OrderflowClient(instrument_id=99)
    provider = DatabentoMinuteProvider("key", client=wrong)
    with pytest.raises(Exception, match="does not match"):
        provider.collect_orderflow(DAY, 42, 1, tmp_path / "instrument")
    assert not list((tmp_path / "instrument").glob("*.gz"))


def test_orderflow_low_disk_refuses_before_any_billable_request(tmp_path, monkeypatch):
    client = OrderflowClient()
    provider = DatabentoMinuteProvider("key", client=client)
    monkeypatch.setattr(
        "backend.research.opening_nq_qqq_forward.shutil.disk_usage",
        lambda _path: SimpleNamespace(
            free=MAX_ORDERFLOW_BYTES + MIN_FREE_DISK_HEADROOM_BYTES - 1
        ),
    )
    with pytest.raises(BudgetRefusal, match="256 MiB"):
        provider.collect_orderflow(DAY, 42, 1, tmp_path / "low-disk")
    assert client.calls == []


def test_nonqualifying_day_records_both_direction_qqq_null_baseline(tmp_path):
    store = make_store(tmp_path)
    nq = NQProvider(current_close=101.0)
    qqq = QQQProvider()
    result = observe_session(DAY, store, nq, qqq, AFTER_CLOSE)
    assert result["status"] == "NO_SIGNAL"
    assert result["gap_pct"] == pytest.approx(1.0)
    expected_features = gap_prediction_features(1.0)
    assert result["prediction_features"]["absolute_gap_pct"] == pytest.approx(
        expected_features["absolute_gap_pct"]
    )
    assert result["prediction_features"]["gap_strength_ratio"] == pytest.approx(
        expected_features["gap_strength_ratio"]
    )
    assert result["prediction_features"]["gap_strength_ratio"] < 1.0
    assert result["prediction_features"]["may_change_position_size"] is False
    assert qqq.calls == 1
    assert qqq.pre_entry_calls == 1
    pre_entry = result["prediction_features"]["qqq_pre_entry_bbo"]
    assert pre_entry == qqq_pre_entry_features(qqq.pre_entry_value, result["gap_pct"])
    assert pre_entry["status"] == "COMPLETE"
    assert pre_entry["mark_et"] == "09:29:55"
    assert pre_entry["observed_at"] == result["qqq_pre_entry_bbo"]["ts"]
    assert pre_entry["spread"] == pytest.approx(0.02)
    assert pre_entry["spread_bps"] == pytest.approx(2.00020002)
    assert pre_entry["queue_imbalance"] == pytest.approx(-1 / 21)
    assert pre_entry["gap_aligned_queue_imbalance"] == pytest.approx(-1 / 21)
    assert pre_entry["fade_aligned_queue_imbalance"] == pytest.approx(1 / 21)
    assert pre_entry["available_before_entry"] is True
    assert pre_entry["contains_post_entry_information"] is False
    assert pre_entry["may_affect_signal_or_primary_outcome"] is False
    baseline = result["qqq_null_baseline"]
    assert baseline["entry"]["long_gross_per_share"] == pytest.approx(0.18)
    assert baseline["entry"]["short_gross_per_share"] == pytest.approx(-0.22)
    assert baseline["entry"]["long_primary_net_per_share"] == pytest.approx(0.16)
    assert baseline["entry"]["short_primary_net_per_share"] == pytest.approx(-0.24)
    assert baseline["midpoint_change"] == pytest.approx(0.20)
    assert baseline["direction_selected"] is False
    assert store.session(DAY)["attempt_count"] == 1
    summary = store.summary()["qqq_null_baseline"]
    assert summary["all_valid_sessions"] == 1
    assert summary["no_signal_sessions"] == 1
    assert summary["direction_selected"] is False


def test_threshold_is_strictly_greater_than(tmp_path):
    store = make_store(tmp_path)
    current = 100.0 * (1.0 + THRESHOLD_PCT / 100.0) - 1e-11
    result = observe_session(DAY, store, NQProvider(current_close=current), QQQProvider(), AFTER_CLOSE)
    assert abs(result["gap_pct"]) <= THRESHOLD_PCT
    assert result["status"] == "NO_SIGNAL"


def test_no_signal_qqq_failure_is_refused_not_silently_dropped(tmp_path):
    store = make_store(tmp_path)
    marks = qqq_marks()
    marks["entry"] = quote(DAY, 9, 30, 3, 100.0, 100.02, microsecond=1)
    result = observe_session(
        DAY, store, NQProvider(current_close=101.0), QQQProvider(marks), AFTER_CLOSE
    )
    assert result["status"] == "REFUSED_QQQ_SOURCE"
    assert result["gap_pct"] == pytest.approx(1.0)
    assert "qqq_null_baseline" not in result


def test_long_crosses_ask_to_bid_and_subtracts_primary_cost(tmp_path):
    store = make_store(tmp_path)
    result = observe_session(DAY, store, NQProvider(current_close=98.0), QQQProvider(), AFTER_CLOSE)
    assert result["status"] == "COMPLETE"
    assert result["direction"] == 1
    assert result["gross_per_share"] == pytest.approx(0.18)
    assert result["primary_net_per_share"] == pytest.approx(0.16)
    assert result["delayed_5_gross_per_share"] == pytest.approx(0.14)
    assert result["delayed_10_gross_per_share"] == pytest.approx(0.09)
    assert result["prediction_features"]["absolute_gap_pct"] == pytest.approx(2.0)
    assert result["prediction_features"]["gap_strength_ratio"] == pytest.approx(
        2.0 / THRESHOLD_PCT
    )
    assert result["prediction_features"]["qqq_pre_entry_bbo"][
        "fade_aligned_queue_imbalance"
    ] == pytest.approx(-1 / 21)
    assert result["execution_authorized"] is False


def test_short_crosses_bid_to_ask(tmp_path):
    store = make_store(tmp_path)
    result = observe_session(
        DAY, store, NQProvider(current_close=102.0), QQQProvider(qqq_marks(short=True)), AFTER_CLOSE
    )
    assert result["status"] == "COMPLETE"
    assert result["direction"] == -1
    assert result["gross_per_share"] == pytest.approx(0.18)
    assert result["primary_net_per_share"] == pytest.approx(0.16)


def test_orderflow_is_diagnostic_and_cannot_change_primary_result(tmp_path):
    measurements = {
        "status": "COMPLETE", "diagnostic_only": True,
        "may_affect_signal_or_primary_outcome": False,
        "windows": {"first_10_seconds": {"fade_aligned_trade_imbalance": -0.99}},
    }
    with_flow_store = ForwardStore(tmp_path / "with.sqlite3")
    with_flow = observe_session(
        DAY, with_flow_store, NQProvider(orderflow_result=measurements), QQQProvider(),
        AFTER_CLOSE, orderflow_raw_dir=tmp_path / "raw",
    )
    without_flow = observe_session(
        DAY, ForwardStore(tmp_path / "without.sqlite3"), NQProvider(), QQQProvider(),
        AFTER_CLOSE,
    )
    assert with_flow["status"] == "COMPLETE"
    assert with_flow["direction"] == without_flow["direction"] == 1
    assert with_flow["gross_per_share"] == without_flow["gross_per_share"]
    assert with_flow["primary_net_per_share"] == without_flow["primary_net_per_share"]
    assert with_flow["orderflow_diagnostics"] == measurements


def test_orderflow_outage_preserves_complete_qqq_primary(tmp_path):
    nq = NQProvider(orderflow_error=RuntimeError("MBP feed unavailable"))
    result = observe_session(
        DAY, make_store(tmp_path), nq, QQQProvider(), AFTER_CLOSE,
        orderflow_raw_dir=tmp_path / "raw",
    )
    assert result["status"] == "COMPLETE"
    assert result["primary_net_per_share"] == pytest.approx(0.16)
    diagnostic = result["orderflow_diagnostics"]
    assert diagnostic["status"] == "REFUSED_ORDERFLOW_SOURCE"
    assert diagnostic["may_affect_signal_or_primary_outcome"] is False


def test_pre_entry_qqq_outage_preserves_frozen_primary_and_signal(tmp_path):
    qqq = QQQProvider(pre_entry_error=RuntimeError("premarket quote unavailable"))
    result = observe_session(DAY, make_store(tmp_path), NQProvider(), qqq, AFTER_CLOSE)
    assert result["status"] == "COMPLETE"
    assert result["direction"] == 1
    assert result["primary_net_per_share"] == pytest.approx(0.16)
    assert "qqq_pre_entry_bbo" not in result
    diagnostic = result["prediction_features"]["qqq_pre_entry_bbo"]
    assert diagnostic["status"] == "REFUSED_QQQ_PRE_ENTRY_SOURCE"
    assert "premarket quote unavailable" in diagnostic["reason"]
    assert diagnostic["may_affect_signal_or_primary_outcome"] is False
    assert diagnostic["may_change_position_size"] is False


def test_post_entry_qqq_poison_cannot_change_pre_entry_predictor_or_direction(tmp_path):
    normal = observe_session(
        DAY, ForwardStore(tmp_path / "normal.sqlite3"), NQProvider(), QQQProvider(),
        AFTER_CLOSE,
    )
    poisoned_marks = qqq_marks()
    poisoned_marks.update({
        "entry": quote(DAY, 9, 30, 1, 500.00, 500.02),
        "delayed_5": quote(DAY, 9, 30, 6, 700.00, 700.02),
        "delayed_10": quote(DAY, 9, 30, 11, 900.00, 900.02),
        "exit": quote(DAY, 9, 32, 1, 1_000.00, 1_000.02),
    })
    poisoned = observe_session(
        DAY, ForwardStore(tmp_path / "poisoned.sqlite3"), NQProvider(),
        QQQProvider(poisoned_marks), AFTER_CLOSE,
    )
    assert poisoned["direction"] == normal["direction"] == 1
    assert poisoned["prediction_features"]["qqq_pre_entry_bbo"] == normal[
        "prediction_features"
    ]["qqq_pre_entry_bbo"]
    assert poisoned["primary_net_per_share"] != normal["primary_net_per_share"]


def test_no_signal_never_purchases_orderflow(tmp_path):
    nq = NQProvider(current_close=101.0, orderflow_error=AssertionError("must not run"))
    result = observe_session(
        DAY, make_store(tmp_path), nq, QQQProvider(), AFTER_CLOSE,
        orderflow_raw_dir=tmp_path / "raw",
    )
    assert result["status"] == "NO_SIGNAL"
    assert not any(call[0] == "orderflow" for call in nq.calls)


def test_frozen_orderflow_windows_report_effort_pressure_and_reversal():
    rows = []
    for second in range(-5, 121):
        ts = at(DAY, 9, 30, 0) + timedelta(seconds=second)
        open_mid = 100.0 + second * 0.01
        rows.append({
            "timestamp_utc": ts.astimezone(timezone.utc).isoformat(),
            "events": 5, "trades": 1,
            "buy_volume": 2.0, "sell_volume": 1.0,
            "ofi": 3.0, "mean_depth": 10.0,
            "mean_queue_imbalance": 0.2, "mean_spread": 0.25,
            "mean_microprice_displacement": 0.05,
            "open_mid": open_mid, "close_mid": open_mid + 0.005,
        })
    result = summarize_orderflow(rows, DAY, fade_direction=-1)
    pre_entry = result["pre_entry_prediction_inputs"]
    assert pre_entry["covered_seconds"] == 6
    assert pre_entry["window_et"] == ["09:29:55", "09:30:01"]
    assert pre_entry["available_before_entry"] is True
    assert pre_entry["contains_post_entry_information"] is False
    first = result["windows"]["first_10_seconds"]
    assert first["covered_seconds"] == 10
    assert first["events"] == 50
    assert first["buy_volume"] == 20
    assert first["sell_volume"] == 10
    assert first["signed_trade_imbalance"] == pytest.approx(1 / 3)
    assert first["fade_aligned_trade_imbalance"] == pytest.approx(-1 / 3)
    assert first["depth_normalized_ofi"] == pytest.approx(3.0)
    assert first["fade_aligned_depth_normalized_ofi"] == pytest.approx(-3.0)
    assert first["gap_direction_progress_per_contract"] > 0
    assert first["available_before_entry"] is False
    assert first["contains_post_entry_information"] is True
    assert result["may_affect_signal_or_primary_outcome"] is False


def test_orderflow_rows_after_primary_horizon_cannot_change_diagnostics():
    rows = []
    for second in range(-5, 126):
        ts = at(DAY, 9, 30, 0) + timedelta(seconds=second)
        rows.append({
            "timestamp_utc": ts.astimezone(timezone.utc).isoformat(),
            "events": 1, "trades": 1,
            "buy_volume": 1.0 if second <= 120 else 1_000_000.0,
            "sell_volume": 0.0,
            "ofi": 1.0 if second <= 120 else 1_000_000.0,
            "mean_depth": 10.0, "mean_queue_imbalance": 0.1,
            "mean_spread": 0.25, "mean_microprice_displacement": 0.01,
            "open_mid": 100.0,
            "close_mid": 101.0 if second <= 120 else 10_000.0,
        })
    with_future = summarize_orderflow(rows, DAY, fade_direction=-1)
    without_future = summarize_orderflow(rows[:126], DAY, fade_direction=-1)
    assert with_future == without_future


def test_post_entry_poison_cannot_change_pre_entry_prediction_inputs():
    rows = []
    for second in range(-5, 121):
        ts = at(DAY, 9, 30, 0) + timedelta(seconds=second)
        poisoned = second >= 1
        rows.append({
            "timestamp_utc": ts.astimezone(timezone.utc).isoformat(),
            "events": 1, "trades": 1,
            "buy_volume": 1_000_000.0 if poisoned else 2.0,
            "sell_volume": 0.0 if poisoned else 1.0,
            "ofi": 1_000_000.0 if poisoned else 3.0,
            "mean_depth": 10.0, "mean_queue_imbalance": 0.1,
            "mean_spread": 0.25, "mean_microprice_displacement": 0.01,
            "open_mid": 10_000.0 if poisoned else 100.0,
            "close_mid": 20_000.0 if poisoned else 100.01,
        })
    poisoned = summarize_orderflow(rows, DAY, fade_direction=-1)
    clean_pre_entry = summarize_orderflow(rows[:6] + [
        dict(row, buy_volume=1.0, ofi=1.0, open_mid=100.0, close_mid=100.0)
        for row in rows[6:]
    ], DAY, fade_direction=-1)
    assert poisoned["pre_entry_prediction_inputs"] == clean_pre_entry[
        "pre_entry_prediction_inputs"
    ]


def test_summary_reports_gap_strength_only_after_forward_sample(tmp_path):
    store = make_store(tmp_path)
    observations = [
        (date(2026, 8, 19), 1.4, -0.05, -0.5),
        (date(2026, 8, 20), 1.8, 0.10, 0.0),
        (date(2026, 8, 21), 2.2, 0.25, 0.5),
    ]
    for session_day, gap, outcome, queue in observations:
        payload = _base_payload(session_day, "COMPLETE")
        prediction_features = gap_prediction_features(gap)
        prediction_features["qqq_pre_entry_bbo"] = {
            "status": "COMPLETE",
            "fade_aligned_queue_imbalance": queue,
            "may_affect_signal_or_primary_outcome": False,
            "may_change_position_size": False,
        }
        payload.update({
            "gap_pct": gap,
            "direction": -1,
            "gross_per_share": outcome + 0.02,
            "primary_net_per_share": outcome,
            "prediction_features": prediction_features,
        })
        assert store.record(payload, AFTER_CLOSE + timedelta(days=(session_day - DAY).days))
    evidence = store.summary()["gap_strength_forward_evidence"]
    assert evidence["n"] == 3
    assert evidence["spearman_with_primary_net_per_share"] == pytest.approx(1.0)
    assert evidence["status"] == "INSUFFICIENT_FORWARD_SAMPLE"
    assert evidence["threshold_search_permitted"] is False
    assert evidence["may_change_position_size"] is False
    assert evidence["execution_authorized"] is False
    qqq_evidence = store.summary()["qqq_pre_entry_forward_evidence"]
    assert qqq_evidence["n"] == 3
    assert qqq_evidence[
        "spearman_fade_aligned_queue_imbalance_with_primary_net_per_share"
    ] == pytest.approx(1.0)
    assert qqq_evidence["status"] == "INSUFFICIENT_FORWARD_SAMPLE"
    assert qqq_evidence["univariate_descriptive_only"] is True
    assert qqq_evidence["threshold_search_permitted"] is False
    assert qqq_evidence["may_change_position_size"] is False
    assert store.summary()["qqq_pre_entry_diagnostic_counts"] == {"COMPLETE": 3}


def test_roll_transition_is_explicit_refusal_and_never_fetches_qqq(tmp_path):
    store = make_store(tmp_path)
    qqq = QQQProvider()
    result = observe_session(
        DAY, store, NQProvider(current_close=98.0, current_id=43), qqq, AFTER_CLOSE
    )
    assert result["status"] == "REFUSED_ROLL_TRANSITION"
    assert qqq.calls == 0
    assert len(store.events(DAY)) == 1


def test_invalid_or_late_qqq_quote_refuses_without_imputation(tmp_path):
    store = make_store(tmp_path)
    marks = qqq_marks()
    marks["entry"] = quote(DAY, 9, 30, 3, 100.0, 100.02, microsecond=1)
    result = observe_session(DAY, store, NQProvider(), QQQProvider(marks), AFTER_CLOSE)
    assert result["status"] == "REFUSED_QQQ_SOURCE"
    assert "delay" in result["reason"]
    assert "gross_per_share" not in result


def test_source_refusal_is_retained_then_retry_can_complete(tmp_path):
    store = make_store(tmp_path)

    class Down:
        def fetch_bar(self, *_args):
            raise RuntimeError("temporary source outage")

    refused = observe_session(DAY, store, Down(), QQQProvider(), AFTER_CLOSE)
    assert refused["status"] == "REFUSED_NQ_SOURCE"
    complete = observe_session(
        DAY, store, NQProvider(), QQQProvider(), AFTER_CLOSE + timedelta(minutes=1)
    )
    assert complete["status"] == "COMPLETE"
    assert store.session(DAY)["attempt_count"] == 2
    assert [row["event_type"] for row in store.events(DAY)] == [
        "REFUSED_NQ_SOURCE", "COMPLETE",
    ]


def test_restart_after_terminal_result_makes_zero_provider_calls(tmp_path):
    store = make_store(tmp_path)
    first = observe_session(DAY, store, NQProvider(), QQQProvider(), AFTER_CLOSE)
    provider = ExplodingProvider()
    second = observe_session(DAY, store, provider, provider, AFTER_CLOSE + timedelta(hours=1))
    assert second == first
    assert provider.calls == 0
    assert len(store.events(DAY)) == 1


def test_conflicting_terminal_result_is_rejected(tmp_path):
    store = make_store(tmp_path)
    payload = _base_payload(DAY, "NO_SIGNAL")
    payload["gap_pct"] = 0.5
    assert store.record(payload, AFTER_CLOSE)
    changed = dict(payload, gap_pct=0.6)
    with pytest.raises(ForwardRefusal, match="conflicting"):
        store.record(changed, AFTER_CLOSE + timedelta(minutes=1))


def test_event_ledger_is_append_only_and_contains_no_execution_fields(tmp_path):
    store = make_store(tmp_path)
    result = observe_session(DAY, store, NQProvider(), QQQProvider(), AFTER_CLOSE)
    text = json.dumps(result).lower()
    for forbidden in ("api_key", "secret", "order_id", "quantity", "account_id", "broker_submit"):
        assert forbidden not in text
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.conn.execute("DELETE FROM nq_qqq_forward_events")
    summary = store.summary()
    assert summary["contract_sha256"] == CONTRACT_SHA256
    assert summary["first_eligible_session"] == str(FIRST_ELIGIBLE_SESSION)
    assert summary["execution_authorized"] is False


def test_module_does_not_import_production_or_execution_modules():
    path = __import__(
        "backend.research.opening_nq_qqq_forward", fromlist=["__file__"]
    ).__file__
    source = open(path).read()
    for forbidden in (
        "backend.main", "backend.trading", "execution_safety", "backend.patterns",
        "alpaca.trading", "submit_order", "patterns.db",
    ):
        assert forbidden not in source
