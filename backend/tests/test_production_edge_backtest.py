from datetime import datetime, timedelta, timezone

import pytest

from backend.research.production_edge_backtest import (
    Bar,
    Signal,
    StrategyConfig,
    _purge_training,
    aggregate_bars,
    chronological_groups,
    cpcv_6x2_splits,
    load_bars_csv,
    load_point_in_time_news,
    compute_signal_candidates,
    simulate_trades,
)


def _bars(count=120, symbol="QQQ"):
    start = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
    return [
        Bar(start + timedelta(minutes=30 * index), symbol, 100, 101, 99, 100, 10)
        for index in range(count)
    ]


def _signal(index=0, side=1):
    bars = _bars()
    return Signal(index, bars[index].timestamp, "QQQ", side, 0.5, "trending", "trend_following", "p")


def test_cpcv_6x2_has_exactly_15_unique_test_pairs():
    bars = _bars(60)
    splits = cpcv_6x2_splits(range(60), bars, purge_bars=1, embargo_bars=1)
    assert len(splits) == 15
    assert len({split.split_id for split in splits}) == 15
    assert all(split.train_indices and split.test_indices for split in splits)
    assert all(not (set(split.train_indices) & set(split.test_indices)) for split in splits)


def test_purge_removes_training_observations_that_can_overlap_test_horizon():
    bars = _bars(20)
    kept, removed = _purge_training(set(range(20)) - {10}, {10}, bars, 3, 2)
    assert removed == {7, 8, 9, 11, 12}
    assert not (removed & kept)


def test_next_bar_open_is_used_and_round_trip_cost_reduces_return():
    bars = _bars(4)
    bars[1] = Bar(bars[1].timestamp, "QQQ", 102, 103, 101, 102, 10)
    config_free = StrategyConfig(max_hold_bars=1, round_trip_cost_bps=0, slippage_bps_per_side=0)
    config_cost = StrategyConfig(max_hold_bars=1, round_trip_cost_bps=10, slippage_bps_per_side=0)
    free = simulate_trades(bars, [_signal()], config_free)[0]
    cost = simulate_trades(bars, [_signal()], config_cost)[0]
    assert free.entry_price == 102
    assert cost.net_return == pytest.approx(free.net_return - 0.001)


def test_when_stop_and_target_touch_same_bar_stop_wins_conservatively():
    bars = _bars(3)
    bars[1] = Bar(bars[1].timestamp, "QQQ", 100, 104, 97, 100, 10)
    trade = simulate_trades(
        bars,
        [_signal()],
        StrategyConfig(max_hold_bars=1, round_trip_cost_bps=0, slippage_bps_per_side=0),
    )[0]
    assert trade.reason == "stop"
    assert trade.gross_return == pytest.approx(-0.025)


def test_future_news_is_rejected(tmp_path):
    path = tmp_path / "news.csv"
    path.write_text(
        "decision_timestamp,observed_at,symbol,score,source\n"
        "2026-01-02T14:30:00Z,2026-01-02T14:31:00Z,QQQ,0.2,wire\n"
    )
    with pytest.raises(ValueError, match="after decision"):
        load_point_in_time_news(path)


def test_bar_loader_rejects_duplicate_timestamp(tmp_path):
    path = tmp_path / "bars.csv"
    path.write_text(
        "timestamp,symbol,open,high,low,close,volume\n"
        "2026-01-02T14:30:00Z,QQQ,100,101,99,100,1\n"
        "2026-01-02T14:30:00Z,QQQ,100,101,99,100,1\n"
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_bars_csv(path)


def test_aggregation_preserves_ohlcv_order():
    start = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
    bars = [
        Bar(start, "QQQ", 100, 102, 99, 101, 5),
        Bar(start + timedelta(minutes=1), "QQQ", 101, 104, 100, 103, 7),
    ]
    result = aggregate_bars(bars, 30)
    assert result == [Bar(start, "QQQ", 100, 104, 99, 103, 12)]


def test_signal_candidates_are_regular_hours_only(monkeypatch):
    bars = _bars(220)
    monkeypatch.setattr(
        "backend.research.production_edge_backtest.production_patterns.compute_rsi",
        lambda *_args, **_kwargs: 20.0,
    )
    monkeypatch.setattr(
        "backend.research.production_edge_backtest.production_patterns.compute_adx",
        lambda *_args, **_kwargs: 10.0,
    )
    monkeypatch.setattr(
        "backend.research.production_edge_backtest.production_patterns.compute_ema",
        lambda prices, period: 101.0 if period == 20 else 100.0,
    )
    monkeypatch.setattr(
        "backend.research.production_edge_backtest.production_patterns.realized_volatility_pct",
        lambda *_args, **_kwargs: 1.0,
    )
    signals = compute_signal_candidates(bars, StrategyConfig())
    assert signals
    eastern = __import__("zoneinfo").ZoneInfo("America/New_York")
    entry_times = [bars[signal.index + 1].timestamp.astimezone(eastern) for signal in signals]
    assert all(
        9 * 60 + 30 <= timestamp.hour * 60 + timestamp.minute < 16 * 60
        for timestamp in entry_times
    )


def test_chronological_groups_never_split_symbols_at_same_timestamp():
    first = _bars(12, "QQQ")
    second = [
        Bar(bar.timestamp, "SPY", bar.open, bar.high, bar.low, bar.close, bar.volume)
        for bar in first
    ]
    bars = sorted(first + second, key=lambda bar: (bar.timestamp, bar.symbol))
    groups = chronological_groups(range(len(bars)), bars, 6)
    assigned = {}
    for group_number, group in enumerate(groups):
        for index in group:
            timestamp = bars[index].timestamp
            assert timestamp not in assigned or assigned[timestamp] == group_number
            assigned[timestamp] = group_number
