import json
from datetime import date, datetime, timedelta, timezone

import pytest

from backend.research.opening_executable_bbo import AuditRefusal, SignalSession
from backend.research.opening_nq_qqq_bridge import (
    ET,
    EquityQuote,
    _request_first_quote,
    analyze,
    collect,
    first_valid_quote,
    load_session,
    mark_request_params,
    parse_quote,
    selected_quote,
)


def at(day, hour, minute, second, microsecond=0):
    return datetime(day.year, day.month, day.day, hour, minute, second, microsecond, tzinfo=ET)


def api_quote(ts, bid, ask, bid_size=10, ask_size=11):
    return {
        "t": ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "bp": bid, "ap": ask, "bs": bid_size, "as": ask_size, "bx": "Q", "ax": "P",
    }


def stored_quote(ts, bid, ask):
    return {
        "ts": ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "bid": bid, "ask": ask, "bid_size": 10, "ask_size": 11,
        "bid_exchange": "Q", "ask_exchange": "P",
    }


def write_marks(path, day, direction=1):
    if direction > 0:
        prices = {
            "entry": (100.00, 100.02), "delayed_5": (100.04, 100.06),
            "delayed_10": (100.09, 100.11), "exit": (100.20, 100.22),
        }
    else:
        prices = {
            "entry": (100.00, 100.02), "delayed_5": (99.96, 99.98),
            "delayed_10": (99.91, 99.93), "exit": (99.80, 99.82),
        }
    times = {"entry": (9, 30, 1), "delayed_5": (9, 30, 6), "delayed_10": (9, 30, 11), "exit": (9, 32, 1)}
    payload = {
        "day": str(day), "symbol": "QQQ", "feed": "sip", "marks": {
            name: stored_quote(at(day, *times[name], 10_000), *prices[name]) for name in times
        },
    }
    path.write_text(json.dumps(payload) + "\n")


def test_mark_query_is_data_minimized_and_dst_safe():
    winter = mark_request_params(date(2026, 1, 5), (9, 30, 1))
    summer = mark_request_params(date(2026, 7, 6), (9, 30, 1))
    assert winter["start"] == "2026-01-05T14:30:01Z"
    assert winter["end"] == "2026-01-05T14:30:03Z"
    assert summer["start"] == "2026-07-06T13:30:01Z"
    assert winter["feed"] == "sip"
    assert winter["sort"] == "asc"
    assert winter["limit"] == 1


def test_quote_must_be_post_mark_timely_open_and_sized():
    day = date(2026, 1, 5)
    nominal = at(day, 9, 30, 1)
    valid = parse_quote(api_quote(nominal + timedelta(microseconds=1), 100, 100.01), nominal)
    assert valid.bid == 100
    with pytest.raises(AuditRefusal, match="delay"):
        parse_quote(api_quote(nominal - timedelta(microseconds=1), 100, 100.01), nominal)
    with pytest.raises(AuditRefusal, match="delay"):
        parse_quote(api_quote(nominal + timedelta(seconds=2, microseconds=1), 100, 100.01), nominal)
    with pytest.raises(AuditRefusal, match="locked or crossed"):
        parse_quote(api_quote(nominal, 100, 100), nominal)
    with pytest.raises(AuditRefusal, match="price/size"):
        parse_quote(api_quote(nominal, 100, 100.01, bid_size=0), nominal)


def test_selected_quote_requires_exactly_one_even_with_pagination_token():
    day = date(2026, 1, 5)
    nominal = at(day, 9, 30, 1)
    payload = {"quotes": {"QQQ": [api_quote(nominal, 100, 100.01)]}, "next_page_token": "later"}
    assert selected_quote(payload, nominal).ask == 100.01
    with pytest.raises(AuditRefusal, match="found 0"):
        selected_quote({"quotes": {"QQQ": []}}, nominal)


def test_first_valid_quote_skips_locked_quote_but_stays_in_window():
    day = date(2026, 1, 5)
    nominal = at(day, 9, 30, 1)
    payload = {
        "quotes": {"QQQ": [
            api_quote(nominal + timedelta(microseconds=1), 100, 100),
            api_quote(nominal + timedelta(microseconds=2), 100, 100.01),
        ]},
        "next_page_token": "later",
    }
    quote, token = first_valid_quote(payload, nominal)
    assert quote.ask == 100.01
    assert token == "later"


def test_long_and_short_cross_the_correct_sip_sides(tmp_path):
    day = date(2026, 1, 5)
    long_path = tmp_path / "long.json"
    write_marks(long_path, day, 1)
    long = load_session(SignalSession(day, -1.5, 1), long_path)
    assert long.gross_per_share == pytest.approx(0.18)
    assert long.delayed_5_per_share == pytest.approx(0.14)
    assert long.delayed_10_per_share == pytest.approx(0.09)

    short_path = tmp_path / "short.json"
    write_marks(short_path, day, -1)
    short = load_session(SignalSession(day, 1.5, -1), short_path)
    assert short.gross_per_share == pytest.approx(0.18)
    assert short.delayed_5_per_share == pytest.approx(0.14)
    assert short.delayed_10_per_share == pytest.approx(0.09)


class FakeResponse:
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload

    def close(self):
        return None


class FakeClient:
    def __init__(self):
        self.calls = []

    def get(self, _url, headers, params, timeout):
        self.calls.append({"headers": headers, "params": params, "timeout": timeout})
        start = datetime.fromisoformat(str(params["start"]).replace("Z", "+00:00"))
        second = start.astimezone(ET).second
        bid = {1: 100.00, 6: 100.04, 11: 100.09}.get(second, 100.20)
        return FakeResponse({"quotes": {"QQQ": [api_quote(start + timedelta(microseconds=1), bid, bid + 0.02)]}})


class NoWait:
    def wait(self):
        return None


class LockedThenValidClient:
    def __init__(self, nominal):
        self.nominal = nominal
        self.calls = []

    def get(self, _url, headers, params, timeout):
        self.calls.append(dict(params))
        if len(self.calls) == 1:
            return FakeResponse({
                "quotes": {"QQQ": [api_quote(self.nominal, 100, 100)]},
                "next_page_token": "next-valid",
            })
        assert params["page_token"] == "next-valid"
        assert params["limit"] == 100
        return FakeResponse({
            "quotes": {"QQQ": [api_quote(self.nominal + timedelta(microseconds=2), 100, 100.01)]},
            "next_page_token": None,
        })


def test_request_follows_token_only_to_recover_first_valid_quote():
    day = date(2026, 1, 5)
    client = LockedThenValidClient(at(day, 9, 30, 1))
    quote = _request_first_quote(client, {}, day, (9, 30, 1), NoWait())
    assert quote.ask == 100.01
    assert len(client.calls) == 2


def test_collector_requests_only_four_first_quotes_and_stores_no_credentials(tmp_path):
    day = date(2026, 1, 5)
    client = FakeClient()
    manifest = collect(
        [SignalSession(day, -1.5, 1)], tmp_path, "key-id", "secret-key",
        client=client, limiter=NoWait(),
    )
    assert manifest["request_count"] == 4
    assert manifest["complete_sessions"] == 1
    assert len(client.calls) == 4
    assert all(call["params"]["limit"] == 1 for call in client.calls)
    text = (tmp_path / f"qqq_sip_marks_{day}.json").read_text()
    assert "key-id" not in text and "secret-key" not in text
    payload = json.loads(text)
    assert set(payload["marks"]) == {"entry", "delayed_5", "delayed_10", "exit"}


def test_small_sample_cannot_authorize_execution(tmp_path):
    day = date(2026, 1, 5)
    write_marks(tmp_path / f"qqq_sip_marks_{day}.json", day, 1)
    report = analyze([SignalSession(day, -1.5, 1)], tmp_path)
    assert report["research_only"] is True
    assert report["execution_authorized"] is False
    assert report["status"] == "QQQ_EXECUTION_BRIDGE_FAIL"
    assert report["gates"]["at_least_50"] is False
    assert report["primary"]["mean_points"] == pytest.approx(0.16)
