#!/usr/bin/env python3
"""Safely replace fabricated daily bars with Alpaca paper-feed data.

Dry-run is the default. ``--db`` is mandatory and ``--apply`` is the only
mode that can mutate it. This script is never called by the orchestrator.
Validated against alpaca-py 0.43.5 StockBarsRequest (no page_token).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import date, datetime, time, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(REPO_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

START_DATE = date(2024, 7, 8)
SYMBOLS = ("SPY", "QQQ", "IWM")
MAX_BARS_PER_SYMBOL = 10_000


def trading_days(start: date, end: date) -> set[date]:
    from market_clock import MarketClock
    clock = MarketClock()
    days: set[date] = set()
    current = start
    while current <= end:
        if clock.is_trading_day(current):
            days.add(current)
        current += timedelta(days=1)
    return days


def last_closed_trading_day(now: datetime | None = None) -> date:
    """Return the latest fully closed RTH session in market (Central) time."""
    from market_clock import MarketClock
    from market_clock import RTH_CLOSE
    clock = MarketClock()
    current = now.astimezone(timezone.utc) if now is not None and now.tzinfo else now
    if current is None:
        current = datetime.now(timezone.utc)
    market_now = clock.now_ct()
    # Use the supplied instant only for deterministic tests; convert to market tz.
    if now is not None:
        try:
            from zoneinfo import ZoneInfo
            market_now = current.astimezone(ZoneInfo("America/Chicago"))
        except Exception:
            market_now = current
    candidate = market_now.date()
    if not clock.is_trading_day(candidate) or market_now.time() < RTH_CLOSE:
        candidate -= timedelta(days=1)
    while not clock.is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    from market_clock import MarketClock
    clock = MarketClock()
    rows = conn.execute(
        "SELECT symbol,date,open,high,low,close,volume FROM daily_bars ORDER BY symbol,date"
    ).fetchall()
    result = []
    for row in rows:
        try:
            day = date.fromisoformat(row["date"])
        except (TypeError, ValueError):
            result.append(row)
            continue
        fabricated = row["volume"] == 1_000_000 and abs(row["open"] - row["close"] * .99) < .0001
        if fabricated or not clock.is_trading_day(day):
            result.append(row)
    return result


def fetch_bars(symbol: str, start: date, end: date) -> list:
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    key = os.environ.get("APCA_API_KEY_ID", "")
    secret = os.environ.get("APCA_API_SECRET_KEY", "")
    if not key or not secret:
        raise RuntimeError("APCA_API_KEY_ID/APCA_API_SECRET_KEY required to fetch replacement bars")
    client = StockHistoricalDataClient(key, secret)
    request = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame.Day,
        start=datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
        end=datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc),
        feed=DataFeed.IEX,
    )
    response = client.get_stock_bars(request)
    return list(response.data.get(symbol, [])) if hasattr(response, "data") else []


def validate_bars(symbol: str, bars: list, expected: set[date], end: date) -> dict[date, tuple]:
    if not bars or len(bars) > MAX_BARS_PER_SYMBOL:
        raise RuntimeError(f"unsafe bar count for {symbol}: {len(bars)}")
    validated: dict[date, tuple] = {}
    for bar in bars:
        timestamp = getattr(bar, "timestamp", None)
        if timestamp is None:
            raise RuntimeError(f"{symbol}: bar missing timestamp")
        day = timestamp.astimezone(timezone.utc).date() if timestamp.tzinfo else timestamp.date()
        if day < START_DATE or day > end or day not in expected:
            continue
        if day in validated:
            raise RuntimeError(f"{symbol}: duplicate bar for {day}")
        values = (bar.open, bar.high, bar.low, bar.close)
        if any(value is None for value in values):
            raise RuntimeError(f"{symbol}: incomplete OHLC bar for {day}")
        if not (float(bar.low) <= float(bar.open) <= float(bar.high) and float(bar.low) <= float(bar.close) <= float(bar.high)):
            raise RuntimeError(f"{symbol}: invalid OHLC bar for {day}")
        # daily_bars.volume is NOT NULL; map absent provider volume to zero,
        # and never call int(None).
        volume_value = getattr(bar, "volume", None)
        volume = 0 if volume_value is None else int(volume_value)
        if volume < 0:
            raise RuntimeError(f"{symbol}: negative volume for {day}")
        validated[day] = (float(bar.open), float(bar.high), float(bar.low), float(bar.close), volume)
    if len(validated) != len(expected):
        missing = sorted(expected - set(validated))
        raise RuntimeError(f"{symbol}: replacement count {len(validated)} != expected {len(expected)}; missing={missing}")
    return validated


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    end = last_closed_trading_day()
    expected = trading_days(START_DATE, end)
    conn = connect(args.db)
    try:
        # Restrict destructive scope to the explicitly supported symbols;
        # never delete malformed/non-trading rows belonging to other symbols.
        bad = [row for row in candidates(conn) if row["symbol"] in SYMBOLS]
        print(f"mode={'APPLY' if args.apply else 'DRY-RUN'} db={args.db}")
        for symbol in SYMBOLS:
            before = conn.execute("SELECT COUNT(*) FROM daily_bars WHERE symbol=?", (symbol,)).fetchone()[0]
            remove = [row for row in bad if row["symbol"] == symbol]
            existing = set()
            malformed_dates = []
            for row in conn.execute("SELECT date FROM daily_bars WHERE symbol=?", (symbol,)):
                try:
                    parsed = date.fromisoformat(row[0])
                except (TypeError, ValueError):
                    malformed_dates.append(row[0])
                    continue
                if parsed >= START_DATE:
                    existing.add(parsed)
            if malformed_dates:
                raise RuntimeError(
                    f"{symbol}: malformed existing date(s) {malformed_dates!r}; "
                    "repair aborted before fetch; fix schema data and rerun"
                )
            print(f"{symbol}: before={before} candidates_remove={len(remove)} projected_after={before-len(remove)} missing_before={len(expected-existing)}")
        # Fetch and validate every symbol before any deletion, including dry-run.
        fetched: dict[str, dict[date, tuple]] = {}
        for symbol in SYMBOLS:
            fetched[symbol] = validate_bars(symbol, fetch_bars(symbol, START_DATE, end), expected, end)
            print(f"{symbol}: fetched_validated={len(fetched[symbol])} (no writes)")
        if not args.apply:
            return 0
        backup = args.db + ".bak"
        shutil.copy2(args.db, backup)  # one-line pre-apply file-copy backup
        try:
            conn.execute("BEGIN")
            for row in bad:
                conn.execute("DELETE FROM daily_bars WHERE symbol=? AND date=?", (row["symbol"], row["date"]))
            for symbol, bars in fetched.items():
                for day, values in bars.items():
                    conn.execute(
                        "INSERT INTO daily_bars(symbol,date,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?) ON CONFLICT(symbol,date) DO UPDATE SET open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume",
                        (symbol, str(day), *values),
                    )
            for symbol in SYMBOLS:
                got = {date.fromisoformat(row[0]) for row in conn.execute("SELECT date FROM daily_bars WHERE symbol=? AND date>=?", (symbol, str(START_DATE)))}
                missing = sorted(expected - got)
                count = len(got)
                print(f"{symbol}: after={count} missing={[str(day) for day in missing]}")
                if missing or count < len(expected):
                    raise RuntimeError(f"final verification failed for {symbol}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
