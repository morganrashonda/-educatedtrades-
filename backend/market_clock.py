"""
Market Hours Clock for Educated Trades.

Determines whether the US equity market (NYSE/Nasdaq regular trading hours) is
open, so the orchestrator only executes trades during market hours and goes
into "standby" outside them (news/sentiment/pattern prep still runs).

Design notes:
  - All logic is in CT (America/Chicago) per owner directive, via the stdlib
    `zoneinfo`, so DST is handled automatically.
  - Regular Trading Hours (RTH): Mon-Fri, 08:30-15:00 CT (== 09:30-16:00 ET —
    the US equity session expressed in Central Time).
  - Weekends and a hardcoded NYSE holiday calendar are treated as closed. The
    calendar includes fixed + observed holidays and a couple of known early
    closes; it is intentionally simple and easy to extend year by year.
  - Extended-hours trading can be enabled via constructor flag or the
    ALLOW_EXTENDED_HOURS env var (pre-market 03:00 and after-hours to 19:00 CT).

Usage:
    from market_clock import MarketClock
    clock = MarketClock()
    if clock.is_open():
        ...  # trade
    clock.status()  # {"is_open":..., "next_open":..., "next_close":..., ...}
"""

import os
from datetime import datetime, date, time, timedelta, timezone, tzinfo
from typing import Dict, Optional, Set

# Market-hours logic runs in Central Time (owner directive).
MARKET_TZ_NAME = "America/Chicago"


def _us_dst_bounds(year: int):
    """(start, end) of US daylight saving for `year`, in local standard time.

    DST runs from the second Sunday in March at 02:00 to the first Sunday in
    November at 02:00.
    """
    march = date(year, 3, 1)
    first_sunday = march + timedelta(days=(6 - march.weekday()) % 7)
    dst_start = datetime.combine(first_sunday + timedelta(days=7), time(2, 0))
    november = date(year, 11, 1)
    dst_end = datetime.combine(
        november + timedelta(days=(6 - november.weekday()) % 7), time(2, 0))
    return dst_start, dst_end


class _CentralFallback(tzinfo):
    """DST-correct US Central timezone for hosts without the tz database.

    The previous fallback was ``utcnow() - timedelta(hours=6)``: naive (so it
    could not be compared against aware datetimes) and an hour wrong for the
    ~8 months of the year that CDT is in effect. An hour of error at the open
    or close is the difference between trading and not.
    """

    def utcoffset(self, dt):
        return timedelta(hours=-5 if self._is_dst(dt) else -6)

    def dst(self, dt):
        return timedelta(hours=1) if self._is_dst(dt) else timedelta(0)

    def tzname(self, dt):
        return "CDT" if self._is_dst(dt) else "CST"

    @staticmethod
    def _is_dst(dt) -> bool:
        if dt is None:
            return False
        naive = dt.replace(tzinfo=None)
        start, end = _us_dst_bounds(naive.year)
        return start <= naive < end


try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(MARKET_TZ_NAME)
except Exception:  # pragma: no cover - zoneinfo always present on 3.9+
    _TZ = _CentralFallback()

# Regular Trading Hours (CT) — equivalent to 09:30-16:00 ET.
RTH_OPEN = time(8, 30)
RTH_CLOSE = time(15, 0)

# Extended hours (CT) when enabled — equivalent to 04:00-20:00 ET.
EXT_OPEN = time(3, 0)
EXT_CLOSE = time(19, 0)

# NYSE early closes at 13:00 ET (12:00 CT) on a handful of days each year.
# Treating those as full sessions means believing the market is open for three
# hours while it is shut -- orders placed then are rejected or queued into the
# next session's open, and the stop-loss monitor evaluates stale prices.
EARLY_CLOSE = time(12, 0)
EXT_EARLY_CLOSE = time(16, 0)   # extended hours also truncate (17:00 ET)


def _observed(d: date) -> date:
    """Return the NYSE-observed date for a fixed-date holiday.

    Saturday holidays are observed the preceding Friday; Sunday holidays the
    following Monday.
    """
    if d.weekday() == 5:      # Saturday -> Friday
        return d - timedelta(days=1)
    if d.weekday() == 6:      # Sunday -> Monday
        return d + timedelta(days=1)
    return d


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The date of the n-th given weekday (0=Mon) in a month."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The date of the last given weekday (0=Mon) in a month."""
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def nyse_holidays(year: int) -> Set[date]:
    """
    Compute the standard NYSE full-day market holidays for a given year.

    Covers: New Year's Day, MLK Day, Washington's Birthday, Good Friday,
    Memorial Day, Juneteenth, Independence Day, Labor Day, Thanksgiving,
    Christmas. Uses observed dates for fixed holidays landing on a weekend.
    """
    hols: Set[date] = set()
    hols.add(_observed(date(year, 1, 1)))                    # New Year's Day
    hols.add(_nth_weekday(year, 1, 0, 3))                    # MLK (3rd Mon Jan)
    hols.add(_nth_weekday(year, 2, 0, 3))                    # Washington (3rd Mon Feb)
    hols.add(_good_friday(year))                             # Good Friday
    hols.add(_last_weekday(year, 5, 0))                      # Memorial (last Mon May)
    if year >= 2021:
        hols.add(_observed(date(year, 6, 19)))               # Juneteenth
    hols.add(_observed(date(year, 7, 4)))                    # Independence Day
    hols.add(_nth_weekday(year, 9, 0, 1))                    # Labor (1st Mon Sep)
    hols.add(_nth_weekday(year, 11, 3, 4))                   # Thanksgiving (4th Thu Nov)
    hols.add(_observed(date(year, 12, 25)))                  # Christmas
    return hols


def nyse_early_closes(year: int) -> Set[date]:
    """Half-day sessions (close 13:00 ET) for a given year.

    The standard three: the day after Thanksgiving, Christmas Eve, and July 3
    -- each only when it is a weekday and not itself a full holiday.
    """
    holidays = nyse_holidays(year)
    days: Set[date] = set()

    thanksgiving = _nth_weekday(year, 11, 3, 4)
    for candidate in (thanksgiving + timedelta(days=1),
                      date(year, 12, 24),
                      date(year, 7, 3)):
        if candidate.weekday() < 5 and candidate not in holidays:
            days.add(candidate)
    return days


def _good_friday(year: int) -> date:
    """Good Friday = 2 days before Easter Sunday (Anonymous Gregorian algo)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    easter = date(year, month, day)
    return easter - timedelta(days=2)


class MarketClock:
    """US equity market-hours clock (ET). Thread-safe (stateless queries)."""

    def __init__(self, allow_extended_hours: Optional[bool] = None):
        if allow_extended_hours is None:
            allow_extended_hours = os.environ.get(
                "ALLOW_EXTENDED_HOURS", ""
            ).strip().lower() in ("1", "true", "yes", "on")
        self.allow_extended_hours = allow_extended_hours
        self._holiday_cache: Dict[int, Set[date]] = {}
        self._early_close_cache: Dict[int, Set[date]] = {}

    # ------------------------------------------------------------------
    def now_ct(self) -> datetime:
        """Current time in Central Time (market timezone), always aware."""
        return datetime.now(_TZ)

    @staticmethod
    def to_market_time(dt: Optional[datetime]) -> datetime:
        """Normalise any datetime to market-local time.

        Session bounds are wall-clock times in Central. Comparing them against
        a datetime from another zone silently answers a different question --
        a UTC afternoon reads as a Central morning, so is_open() returned the
        exact opposite of the truth. Naive datetimes are assumed to be already
        market-local, which is what internal callers pass.
        """
        if dt is None:
            return datetime.now(_TZ)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=_TZ)
        return dt.astimezone(_TZ)

    # Backwards-compatible alias.
    now_et = now_ct

    def _holidays(self, year: int) -> Set[date]:
        if year not in self._holiday_cache:
            self._holiday_cache[year] = nyse_holidays(year)
        return self._holiday_cache[year]

    def is_holiday(self, d: date) -> bool:
        return d in self._holidays(d.year)

    def _early_closes(self, year: int) -> Set[date]:
        if year not in self._early_close_cache:
            self._early_close_cache[year] = nyse_early_closes(year)
        return self._early_close_cache[year]

    def is_early_close(self, d: date) -> bool:
        """True if `d` is a half session (NYSE closes 13:00 ET / 12:00 CT)."""
        return d in self._early_closes(d.year)

    def is_trading_day(self, d: date) -> bool:
        """True if `d` is a weekday and not an NYSE holiday."""
        return d.weekday() < 5 and not self.is_holiday(d)

    def _session_bounds(self, d: date):
        """(open_time, close_time) for a session.

        Honours extended hours and half-day sessions. `d` matters: an early
        close is date-dependent, which is why this takes the date at all.
        """
        early = self.is_early_close(d)
        if self.allow_extended_hours:
            return EXT_OPEN, (EXT_EARLY_CLOSE if early else EXT_CLOSE)
        return RTH_OPEN, (EARLY_CLOSE if early else RTH_CLOSE)

    # ------------------------------------------------------------------
    def is_open(self, dt: Optional[datetime] = None) -> bool:
        """True if the market is currently open (RTH, or extended if enabled)."""
        dt = self.to_market_time(dt)
        d = dt.date()
        if not self.is_trading_day(d):
            return False
        open_t, close_t = self._session_bounds(d)
        return open_t <= dt.time() < close_t

    def is_premarket(self, dt: Optional[datetime] = None) -> bool:
        """True during the actual pre-market window on a trading day.

        Bounded at the extended-hours open rather than midnight: 3am is not
        pre-market, it is the middle of the night, and treating it as a
        trading-adjacent window invites work when no quotes exist.
        """
        dt = self.to_market_time(dt)
        return (self.is_trading_day(dt.date())
                and EXT_OPEN <= dt.time() < RTH_OPEN)

    def _next_open(self, dt: datetime) -> datetime:
        """Next datetime the market opens (>= dt)."""
        dt = self.to_market_time(dt)
        open_t, _ = self._session_bounds(dt.date())
        # If today is a trading day and we're before the open, it's today.
        if self.is_trading_day(dt.date()) and dt.time() < open_t:
            return dt.replace(
                hour=open_t.hour, minute=open_t.minute, second=0, microsecond=0
            )
        # Otherwise scan forward for the next trading day.
        probe = dt.date() + timedelta(days=1)
        for _ in range(370):
            if self.is_trading_day(probe):
                o, _ = self._session_bounds(probe)
                return datetime.combine(probe, o, tzinfo=dt.tzinfo)
            probe += timedelta(days=1)
        return dt  # unreachable in practice

    def _next_close(self, dt: datetime) -> datetime:
        """Next datetime the market closes (>= dt)."""
        dt = self.to_market_time(dt)
        open_t, close_t = self._session_bounds(dt.date())
        if self.is_trading_day(dt.date()) and dt.time() < close_t:
            return dt.replace(
                hour=close_t.hour, minute=close_t.minute, second=0, microsecond=0
            )
        # Closes at the next trading day's close.
        probe = dt.date() + timedelta(days=1)
        for _ in range(370):
            if self.is_trading_day(probe):
                _, c = self._session_bounds(probe)
                return datetime.combine(probe, c, tzinfo=dt.tzinfo)
            probe += timedelta(days=1)
        return dt

    # ------------------------------------------------------------------
    def status(self, dt: Optional[datetime] = None) -> dict:
        """
        Structured market-hours status for the API/dashboard.

        Returns is_open, a human phase, next_open/next_close (ISO ET),
        seconds until each, and the config in effect.
        """
        # Normalise first: every field below mixes this datetime with values
        # derived from the market timezone, and naive/aware arithmetic raises.
        dt = self.to_market_time(dt)
        is_open = self.is_open(dt)
        _, session_close = self._session_bounds(dt.date())
        if is_open:
            phase = "open"
        elif self.is_premarket(dt):
            phase = "pre_market"
        elif self.is_trading_day(dt.date()) and dt.time() >= session_close:
            phase = "after_hours"
        elif not self.is_trading_day(dt.date()):
            phase = "holiday" if self.is_holiday(dt.date()) else "weekend"
        else:
            phase = "closed"

        next_open = self._next_open(dt)
        next_close = self._next_close(dt)
        return {
            "is_open": is_open,
            "phase": phase,
            "timezone": MARKET_TZ_NAME,
            "now_ct": dt.isoformat(),
            "next_open": next_open.isoformat(),
            "next_close": next_close.isoformat(),
            "seconds_to_open": max(0, int((next_open - dt).total_seconds())),
            "seconds_to_close": max(0, int((next_close - dt).total_seconds())),
            "allow_extended_hours": self.allow_extended_hours,
            "session": "extended" if self.allow_extended_hours else "regular",
        }


# ---------------------------------------------------------------------------
# CLI: quick manual check
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    import sys
    ext = "--extended" in sys.argv
    clock = MarketClock(allow_extended_hours=ext)
    print(json.dumps(clock.status(), indent=2))
