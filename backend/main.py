"""
Backend Orchestrator & API for Educated Trades.

Ties together all backend modules:
  1. Periodically fetch news (News Ingestion)
  2. Analyze sentiment (Sentiment Engine)
  3. Cross-reference with historical data (Pattern Engine)
  4. Execute trades (Trading Engine) — in autonomous mode
  5. Expose an HTTP API for the Frontend Dashboard

Features:
  - Autonomous Mode: when enabled, the system automatically executes
    high-conviction trades without human intervention
  - Manual Mode: evaluates signals but does NOT auto-execute
  - Built-in HTTP API on port 3099 for the frontend dashboard
  - Graceful shutdown and state management

Usage:
    python main.py                    # Start orchestrator (manual mode)
    python main.py --simulate         # Force simulation mode
    python main.py --api-only         # Just start the API server
"""

import hmac
import errno
import json
import math
import logging
import os
import signal
import sys
import threading
import time
import fcntl
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timezone
from market_clock import RTH_CLOSE
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("educator")

# ---------------------------------------------------------------------------
# Imports from sibling modules
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Will be imported lazily to allow standalone doc/builds
_sentiment = None
_patterns = None
_trading = None
_news = None
_stats = None
_monitoring = None
_shadow_forward = None


def _import_monitoring():
    global _monitoring
    if _monitoring is None:
        import monitoring as m
        _monitoring = m
    return _monitoring


def _import_sentiment():
    global _sentiment
    if _sentiment is None:
        import sentiment as s
        _sentiment = s
    return _sentiment


def _import_patterns():
    global _patterns
    if _patterns is None:
        import patterns as p
        _patterns = p
    return _patterns


def _import_trading():
    global _trading
    if _trading is None:
        import trading as t
        _trading = t
    return _trading


def _import_news():
    global _news
    if _news is None:
        import news_ingestion as n
        _news = n
    return _news


def _import_shadow_forward():
    global _shadow_forward
    if _shadow_forward is None:
        import shadow_forward as sf
        _shadow_forward = sf
    return _shadow_forward

def _import_stats(): 
    global _stats 
    if _stats is None: 
        import stats as s 
        _stats = s 
    return _stats 


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Data directory (overridable via DATA_DIR env var)
def _write_json_atomic(path: str, payload, **dump_kwargs) -> None:
    """Write JSON so a concurrent reader never sees a half-written file.

    `open(path, "w")` truncates immediately and a large json.dump takes long
    enough that the API thread, reading the same file, lands in the middle of
    it. Measured on the overnight-risk snapshot: 1541 of 2500 concurrent reads
    raised JSONDecodeError -- 61.6%. The reader cannot tell "the file is
    briefly inconsistent" from "there is no data", which is the worse of the
    two readings for a risk snapshot.

    Write to a temporary file in the SAME directory (so the rename is on one
    filesystem and therefore atomic), fsync it, then replace. A reader sees
    either the whole previous file or the whole new one.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as handle:
        json.dump(payload, handle, **dump_kwargs)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _resolve_data_dir() -> str:
    """Data directory, segregated by trading environment.

    Paper and live must never share a data directory. Paper fills are
    optimistic -- no queue position, no partial fills, and paper accounts are
    IEX-only -- so mixing them into the same ledger, journal and pattern store
    would contaminate exactly the measurements real money exists to produce.
    Slippage in particular would be an average of real and fake fills.

    Derived from the credentials rather than set by hand, because "remember to
    change DATA_DIR before going live" is a step that eventually gets missed,
    and the failure is silent.

    Set DATA_DIR explicitly to override (tests do).

    The resolved value is written back into ``os.environ`` before anything
    else is imported. That is not a convenience -- it is the whole mechanism.
    Twelve modules read ``DATA_DIR`` from the environment at import time and
    each falls back to a DIFFERENT hardcoded default when it is unset
    (``backend/data``, ``/var/lib/educated-trades/data``, ``.``,
    ``/home/team/shared/data``, ``/opt/educated_trades/data``). Deriving the
    directory here without exporting it meant main wrote the heartbeat to
    ``$DATA_ROOT/paper`` while the watchdog looked for it in
    ``/home/team/shared/data``, the decision journal landed in the working
    directory, and paper and live shared every store except main's own files
    -- the exact contamination this function exists to prevent.

    Subprocesses inherit ``os.environ``, so exporting also fixes the watchdog,
    which runs as a separate process and cannot see this module's globals.
    """
    global DATA_DIR_IS_EXPLICIT
    try:
        # One derivation, in trading.py, shared with the health check and the
        # decision journal. Three private copies is three chances to disagree.
        from trading import data_dir_is_explicit, resolve_data_dir
        DATA_DIR_IS_EXPLICIT = data_dir_is_explicit()
        return resolve_data_dir()
    except Exception:
        DATA_DIR_IS_EXPLICIT = bool(os.environ.get("DATA_DIR"))
        return os.environ.get("DATA_DIR") or os.path.join(
            os.environ.get("DATA_ROOT", "/home/team/shared/data"), "paper")


#: Records the value _resolve_data_dir exported, so the derivation can tell
#: its own answer apart from one an operator set by hand. Defined in
#: trading.py alongside the derivation that consults it.
try:
    from trading import DATA_DIR_AUTOSET as _AUTOSET_MARKER
except Exception:  # pragma: no cover - import-environment specific
    _AUTOSET_MARKER = "DATA_DIR_AUTOSET"
#: True when DATA_DIR came from the environment rather than the credentials.
#: An explicit value silences segregation entirely -- paper and live then
#: share one directory -- so preflight says so out loud.
DATA_DIR_IS_EXPLICIT = False

DATA_DIR = _resolve_data_dir()
#: Export before any DATA_DIR-reading module is imported. Verified safe:
#: main's module body imports only stdlib and market_clock, and every other
#: module of ours is imported lazily inside functions (checks DD-1..DD-8).
os.environ["DATA_DIR"] = DATA_DIR
os.environ[_AUTOSET_MARKER] = DATA_DIR
APP_ROOT = os.environ.get("APP_ROOT", os.path.dirname(os.path.abspath(__file__)))
MODE_FILE = os.path.join(DATA_DIR, "orchestrator_mode.txt")
KILLED_STATE_FILE = os.path.join(DATA_DIR, "killed_state")  # Persisted kill flag
DAILY_STATE_FILE = os.path.join(DATA_DIR, "daily_risk_state.json")  # Daily loss limit
APP_ROOT = os.environ.get("APP_ROOT", os.path.dirname(os.path.abspath(__file__)))

# How often to run the full pipeline (seconds)
DEFAULT_POLL_INTERVAL_S = 120   # 2 minutes

# --- Bar timeframe -------------------------------------------------------
# Daily bars gave ~20-60 trades a year across three correlated ETFs, which is
# far too few to ever learn anything: distinguishing a 55% win rate from a
# coin flip needs several hundred trades. 30-60 minute bars give ~8 trades a
# day, which reaches significance in ~50 trading days while keeping cost at
# roughly 9% of the target move. Shorter than ~5 minutes and spread plus
# slippage eats half the move.
BAR_TIMEFRAME_MINUTES = int(os.environ.get("BAR_TIMEFRAME_MINUTES", "30"))
#: A trade opened with less than this long left in the session will probably
#: still be open at the close, so it is sized for gap risk rather than for the
#: intraday stop. Two bars is enough for a signal to resolve or fail.
OVERNIGHT_HORIZON_MINUTES = int(
    os.environ.get("OVERNIGHT_HORIZON_MINUTES", str(BAR_TIMEFRAME_MINUTES * 2)))
#: Daily bars are the legacy mode; 0 or 1440 selects them.
USE_DAILY_BARS = BAR_TIMEFRAME_MINUTES in (0, 1440)
# Minimum conviction threshold to consider a trade
MIN_CONVICTION_THRESHOLD = 0.10
# High conviction threshold for autonomous execution. TradingEngine's final
# actionable gate is 0.30, so the orchestrator must not advertise 0.20 as
# executable and then have the engine silently refuse it.
HIGH_CONVICTION_THRESHOLD = 0.30
# Default API port
API_PORT = int(os.environ.get("API_PORT", "3099"))
#: The control API can start, stop and place trades. It bound to 0.0.0.0 by
#: default, which put `POST /api/execute` and `POST /api/mode` in front of
#: anything that could reach the port. Loopback unless deliberately opened.
API_BIND = os.environ.get("API_BIND", "127.0.0.1")
# Max headlines to fetch per cycle
MAX_HEADLINES = 25

# ---- Position-safety thresholds (fraction of entry price) ----
# Baseline per owner directive (2026-06-30): hard stop-loss at -2.5%,
# take-profit at +3.0%.
def _scaled_risk(daily_pct: float) -> float:
    """Scale a daily-bar risk parameter to the configured timeframe.

    A 2.5% stop is ~2.5 daily sigma on SPY; on a 30-minute bar it is roughly
    ten times the typical move, so it would never trigger -- the position
    would exit on time rather than on thesis, and the stop would be
    decorative. Volatility scales with the square root of time, so the
    parameter scales the same way.
    """
    if USE_DAILY_BARS:
        return daily_pct
    minutes_per_session = 390.0
    return round(daily_pct * math.sqrt(
        max(1.0, BAR_TIMEFRAME_MINUTES) / minutes_per_session), 5)


STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PCT", "") or _scaled_risk(0.025))
TAKE_PROFIT_PCT = float(os.environ.get("TAKE_PROFIT_PCT", "") or _scaled_risk(0.03))
# How often the dedicated fast-track position monitor checks open positions
# for stop/target hits, independent of the (slower) full pipeline cycle.
POSITION_MONITOR_INTERVAL_S = 15
API_AUTH_TOKEN = os.environ.get("API_AUTH_TOKEN", "")
# Consecutive transient API failures tolerated before fail-safe kill.
TRANSIENT_CYCLE_FAILURE_THRESHOLD = max(1, int(os.environ.get("TRANSIENT_CYCLE_FAILURE_THRESHOLD", "5")))

# RSI extremes used by the mean-reversion strategy (range-bound regime) to
# fade moves: buy at/below oversold, sell at/above overbought.
RSI_MEAN_REVERT_OVERSOLD = 30.0
RSI_MEAN_REVERT_OVERBOUGHT = 70.0

# Tier 2 (exploration): small PAPER-only mean-reversion trades taken only in
# the RSI "grey zone" between the live Tier-1 gate above and a looser
# candidate threshold -- i.e. (RSI_MEAN_REVERT_OVERSOLD, TIER_2_RSI_OVERSOLD]
# on the oversold side, [TIER_2_RSI_OVERBOUGHT, RSI_MEAN_REVERT_OVERBOUGHT) on
# the overbought side. These exist only to give
# _generate_tier2_threshold_advisory() real comparison data on whether
# loosening the Tier-1 threshold would still perform acceptably; they never
# change Tier-1 sizing or side selection, and can never place a live order
# (see _tier2_exploration_gate).
TIER_2_RSI_OVERSOLD = 35.0
TIER_2_RSI_OVERBOUGHT = 65.0
TIER_2_RISK_FACTOR = 0.15   # multiplies TIER_1_RISK_PER_TRADE -- deliberately tiny
TIER_2_DAILY_CAP = 3        # max Tier 2 exploration entries per day, all symbols


def tier2_grey_zone_side(rsi_value: float) -> Optional[str]:
    """Which side of the Tier-2 RSI grey zone rsi_value falls in, if any.

    A pure, standalone check so the boundary math (which the pipeline cycle
    consults inline) can be tested without constructing a full cycle.
    """
    if RSI_MEAN_REVERT_OVERSOLD < rsi_value <= TIER_2_RSI_OVERSOLD:
        return "buy"
    if TIER_2_RSI_OVERBOUGHT <= rsi_value < RSI_MEAN_REVERT_OVERBOUGHT:
        return "sell"
    return None

# Shadow-forward cold-start repair. Shadow observations never enter
# pattern_memory and can only qualify a one-share PAPER exploration order.
SHADOW_DB_PATH = os.path.join(DATA_DIR, "shadow_forward.db")
SHADOW_SIGNAL_THRESHOLD = 0.20
SHADOW_PROMOTION_MIN_TRADES = 100
SHADOW_PROMOTION_MIN_DAYS = 20
PAPER_EXPLORATION_DAILY_CAP = 2
PATTERN_EXECUTION_MIN_RESOLVED = 20

# Tier 1 position size (modestly reduced from 0.5%)
TIER_1_RISK_PER_TRADE = 0.004


def regime_info_from_indicators(indicators: dict) -> dict:
    """Build aggregate telemetry plus an independent regime per symbol.

    The aggregate regime is useful on a dashboard, but it must not choose the
    strategy for every instrument. Averaging a trending ETF with a
    range-bound ETF can turn both into a third, fictional market state.
    """
    pmod = _import_patterns()
    detail = {}
    adx_values = []
    for symbol, values in (indicators or {}).items():
        adx = values.get("adx")
        regime = pmod.classify_regime(adx)
        strategy = pmod.get_strategy_for_regime(regime)
        size_factor = pmod.get_position_size_factor(regime)
        detail[symbol] = {
            "adx": adx,
            "regime": regime,
            "strategy": strategy,
            "position_size_factor": size_factor,
        }
        if adx is not None:
            adx_values.append(float(adx))

    avg_adx = round(sum(adx_values) / len(adx_values), 2) if adx_values else None
    aggregate_regime = pmod.classify_regime(avg_adx)
    return {
        "regime": aggregate_regime,
        "adx": avg_adx,
        "strategy": pmod.get_strategy_for_regime(aggregate_regime),
        "position_size_factor": pmod.get_position_size_factor(aggregate_regime),
        "detail": detail,
        "updated_at": time.time(),
    }


def _epoch_timestamp(value) -> Optional[float]:
    """Parse a market-data timestamp without guessing a local timezone."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(
            value, datetime_time.min, tzinfo=timezone.utc)
    elif isinstance(value, str):
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(text)
    else:
        return float(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def learned_pattern_allows_execution(
    conviction: float, resolved_samples: int, minimum_conviction: float,
) -> bool:
    """Normal size requires outcomes, not merely an open pattern record."""
    return (
        int(resolved_samples) >= PATTERN_EXECUTION_MIN_RESOLVED
        and abs(float(conviction)) >= float(minimum_conviction)
    )

# Max daily loss limit (as a percentage, default 3.0%).
# When exceeded, all positions are closed and trading pauses until the next
# trading day. Configurable via env var DAILY_LOSS_LIMIT_PCT.
DAILY_LOSS_LIMIT_PCT = float(os.environ.get("DAILY_LOSS_LIMIT_PCT", "3.0"))

# Minimum bars required for indicator computation (RSI/ADX)
# --- Indicator periods ---------------------------------------------------
# These are BAR counts, not time. On daily bars EMA(50) is ten weeks; on
# 30-minute bars it is about four sessions. Keeping the bar counts constant
# means the indicators describe an intraday horizon, which matches a 30-60
# minute holding period. Deliberately NOT tuned against historical returns:
# optimising periods on past data is the fastest route to a strategy that
# only worked in the past.
EMA_SHORT_PERIOD = int(os.environ.get("EMA_SHORT_PERIOD", "20"))
EMA_LONG_PERIOD = int(os.environ.get("EMA_LONG_PERIOD", "50"))
RSI_PERIOD = int(os.environ.get("RSI_PERIOD", "14"))
ADX_PERIOD = int(os.environ.get("ADX_PERIOD", "14"))
VOLATILITY_PERIOD = 20

# The longest lookback any indicator needs. Was hardcoded at 40 while EMA(50)
# needs 50: between 40 and 49 bars the fetch passed its own check, EMA(50)
# returned None, trend conviction collapsed to 0.0, and the bot silently
# stopped generating trend signals with no error anywhere.
INDICATOR_REQUIRED_BARS = max(
    EMA_LONG_PERIOD, EMA_SHORT_PERIOD, RSI_PERIOD + 1,
    2 * ADX_PERIOD + 1, VOLATILITY_PERIOD + 1)

# Fetch well beyond the minimum. compute_ema seeds with an SMA of the first
# `period` bars and smooths across the rest, so a series only just long enough
# is still mostly seed -- the estimate needs history after it to converge.
INDICATOR_FETCH_BARS = int(os.environ.get(
    "INDICATOR_FETCH_BARS", str(INDICATOR_REQUIRED_BARS * 4)))

# How many days of historical daily bars to backfill on weekends.
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "502"))
# Symbols whose daily bars we backfill so RSI/ADX have real history.
# --- Trading universe ----------------------------------------------------
# SPY/QQQ/IWM alone are ~0.9 correlated: three simultaneous longs are one bet
# counted three times. That caps both the trade rate (one position per symbol,
# so ~2.8 trades/day) and the EFFECTIVE sample size, which is what actually
# determines when a result means anything.
#
# XLE (energy), TLT (long bonds) and GLD (gold) are deliberately cross-sector
# and cross-asset, so they contribute genuinely independent observations
# rather than repeats. All six are liquid enough that the ~0.03% round-trip
# cost assumption roughly holds -- though XLE's spread is wider than SPY's and
# is worth measuring live.
#
# Rate limits are not a constraint here: Alpaca's Basic (IEX) plan allows 200
# requests/minute and six symbols on a 2-15 minute poll uses well under 5.
TRADING_SYMBOLS = [
    s.strip().upper() for s in os.environ.get(
        "TRADING_SYMBOLS", "SPY,QQQ,IWM,XLE,TLT,GLD").split(",") if s.strip()
]
BACKFILL_SYMBOLS = list(TRADING_SYMBOLS)



# ---------------------------------------------------------------------------
# Enums / State
# ---------------------------------------------------------------------------
class OrchestratorMode(Enum):
    MANUAL = "manual"
    AUTONOMOUS = "autonomous"
    STOPPED = "stopped"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    KILLED = "killed"


@dataclass
class PipelineState:
    """Current state of the orchestration pipeline."""

    mode: OrchestratorMode = OrchestratorMode.MANUAL
    running: bool = False
    cycle_count: int = 0
    last_cycle_time: float = 0.0
    last_sentiment_result: Optional[dict] = None
    last_pattern_result: Optional[dict] = None
    last_trade_result: Optional[dict] = None
    market_regime: dict = field(default_factory=lambda: {
        "regime": "unknown", "adx": None, "detail": {},
        "position_size_factor": 1.0, "strategy": "trend_following",
    })
    market_hours: dict = field(default_factory=lambda: {
        "is_open": False, "phase": "unknown",
        "next_open": None, "next_close": None,
    })
    errors: List[str] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)

    # Daily loss limit tracking
    daily_start_date: Optional[str] = None
    daily_starting_equity: Optional[float] = None
    daily_pnl_pct: float = 0.0
    daily_loss_hit: bool = False
    peak_equity: float = 0.0
    max_drawdown_pct: float = 0.0
    position_size_multiplier: float = 1.0
    tier_2_trades_today: int = 0
    tier_2_total_trades: int = 0
    tier_2_eval_cycle: int = 0
    signal_trade_count: int = 0
    paper_exploration_trades_today: int = 0
    paper_exploration_total_trades: int = 0
    drawdown_killed: bool = False
    killed: bool = False  # KILLED state flag — blocks trading, persisted to KILLED_STATE file
    health_failed_this_session: bool = False  # Pre-market health FAIL advisory, resets on phase change
    startup_recovery_blocked: bool = False  # Broker truth unavailable; retry before trading
    equity_read_failures: int = 0  # Consecutive failed equity reads (glitch detection, resets on success)
    #: symbol -> consecutive checks where the stop could not be evaluated
    unprotected: Dict[str, int] = field(default_factory=dict)
    #: symbol -> best/worst price seen while the position was open (MFE/MAE)
    excursions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: result of the startup safety checks
    preflight: Dict[str, Any] = field(default_factory=dict)
    #: result of the most recent automatic daily review
    last_review: Dict[str, Any] = field(default_factory=dict)
    #: whether the forward-test gate was met at the last review
    forward_ready: bool = False
    consecutive_ref_price_failures: int = 0
    consecutive_transient_cycle_failures: int = 0
    news_fetch_degraded: bool = False
    news_categories_attempted: int = 0
    news_categories_failed: int = 0
    news_headlines_retrieved: int = 0
    news_articles_retrieved_total: int = 0
    news_headlines_used: int = 0
    live_indicators: dict = field(default_factory=dict)
    indicators_valid: bool = False
    refusal_counts: Dict[str, int] = field(default_factory=dict)
    latest_refusals: Dict[str, int] = field(default_factory=dict)

    # Historical data backfill
    backfill_done: bool = False

    # Daily backup dedup
    backup_date: Optional[str] = None

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self.start_time

    @property
    def is_autonomous(self) -> bool:
        return self.mode == OrchestratorMode.AUTONOMOUS and not self.killed

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "running": self.running,
            "cycle_count": self.cycle_count,
            "last_cycle_time": self.last_cycle_time,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "market_regime": self.market_regime,
            "market_hours": self.market_hours,
            "error_count": len(self.errors),
            "recent_errors": self.errors[-5:],
            "daily_pnl_pct": self.daily_pnl_pct,
            "daily_loss_limit_pct": DAILY_LOSS_LIMIT_PCT,
            "daily_loss_hit": self.daily_loss_hit,
            "daily_start_date": self.daily_start_date,
            "peak_equity": self.peak_equity,
            "max_drawdown_pct": self.max_drawdown_pct,
            "position_size_multiplier": self.position_size_multiplier,
            "unprotected_positions": dict(self.unprotected or {}),
            "preflight": dict(self.preflight or {}),
            "drawdown_killed": self.drawdown_killed,
            "killed": self.killed,
            "health_failed_this_session": self.health_failed_this_session,
            "tier_2_trades_today": self.tier_2_trades_today,
            "tier_2_total_trades": self.tier_2_total_trades,
            "tier_2_eval_cycle": self.tier_2_eval_cycle,
            "paper_exploration_trades_today": self.paper_exploration_trades_today,
            "paper_exploration_total_trades": self.paper_exploration_total_trades,
            "kill_switch_active": bool(self.mode == OrchestratorMode.KILLED),
            "consecutive_ref_price_failures": self.consecutive_ref_price_failures,
            "consecutive_transient_cycle_failures": self.consecutive_transient_cycle_failures,
            "news_fetch_degraded": self.news_fetch_degraded,
            "news_categories_attempted": self.news_categories_attempted,
            "news_categories_failed": self.news_categories_failed,
            "news_headlines_retrieved": self.news_headlines_retrieved,
            "news_articles_retrieved_total": self.news_articles_retrieved_total,
            "news_headlines_used": self.news_headlines_used,
            "indicators_valid": self.indicators_valid,
            "live_indicators": self.live_indicators,
            "refusal_counts": dict(self.refusal_counts or {}),
            "latest_refusals": dict(self.latest_refusals or {}),
        }


# ---------------------------------------------------------------------------
# HTTP API Server
# ---------------------------------------------------------------------------
class APIHandler(BaseHTTPRequestHandler):
    """Simple HTTP request handler that serves the orchestrator's data."""

    # Reference to the orchestrator (set externally)
    orchestrator_ref: Optional['Orchestrator'] = None

    def do_GET(self):
        # Fail closed: an unset token refuses every request rather than
        # disabling authentication.
        if not _authorized(self):
            self._send_json(401, {"error": "Unauthorized — provide Authorization: Bearer <token>"})
            return

        try:
            handler = self._route()
            if handler:
                data = handler()
                self._send_json(200, data)
            else:
                self._send_json(404, {"error": "Not found", "path": self.path})
        except Exception as e:
            logger.error("API error: %s", e)
            self._send_json(500, {"error": str(e)})

    def do_POST(self):
        # Fail closed: an unset token refuses every request rather than
        # disabling authentication.
        if not _authorized(self):
            self._send_json(401, {"error": "Unauthorized — provide Authorization: Bearer <token>"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}

        handler = self._route(method="POST")
        if handler:
            try:
                data = handler(payload)
                self._send_json(200, data)
            except Exception as e:
                self._send_json(400, {"error": str(e)})
        else:
            self._send_json(404, {"error": "Not found", "path": self.path})

    def _route(self, method="GET"):
        """Route the request path to the appropriate handler."""
        orch = self.orchestrator_ref
        if not orch:
            return None

        path = self.path.rstrip("/")

        routes: Dict[str, Dict[str, Callable]] = {
            "GET": {
                "/api/status": lambda: {
                    "service": "Educated Trades Backend",
                    "version": "1.0.0",
                    "status": "running",
                    "orchestrator": orch.get_state(),
                    "daily_pnl_pct": orch.state.daily_pnl_pct,
                    "daily_loss_limit": DAILY_LOSS_LIMIT_PCT,
                    "daily_loss_hit": orch.state.daily_loss_hit,
                    "kill_switch_active": bool(orch.state.mode == OrchestratorMode.KILLED
                        or os.path.exists(os.path.join(DATA_DIR, "KILL_SWITCH"))),
                },
                "/api/sentiment/latest": lambda: orch.get_sentiment_data(),
                "/api/patterns/top": lambda: orch.get_pattern_data(),
                "/api/trades/recent": lambda: {"trades": orch.get_trade_history()},
                # The decision journal, which is the thing worth watching:
                # it records why a trade was taken AND why one was refused.
                # The P&L cannot answer either question, and an operator
                # monitoring remotely had no way to see refusals at all.
                "/api/decisions": lambda: orch.get_decisions(),
                "/api/portfolio": lambda: orch.get_portfolio_data(),
                "/api/evaluate": lambda: orch.evaluate_now(),
                "/api/config": lambda: orch.get_config(),
                "/api/learning/report": lambda: orch.get_learning_report(),
                "/api/milestones": lambda: orch.get_milestones(),
                "/api/monitoring": lambda: orch.get_monitoring_status(),
                "/api/regime": lambda: orch.get_market_regime(),
                "/api/market-hours": lambda: orch.get_market_hours(),
                "/api/audit/recent": lambda: orch.get_recent_audit(),
                "/api/alerts/recent": lambda: orch.get_recent_alerts(),
                "/api/alerts": lambda: orch.get_recent_alerts(),
                "/api/reconciliation/latest": lambda: orch.get_latest_reconciliation(),
                "/api/health/pre-market": lambda: orch.get_pre_market_health(),
                "/api/heartbeat": lambda: orch.get_heartbeat(),
                "/api/risk/overnight": lambda: orch.get_overnight_risk(),
                "/api/backfill/status": lambda: orch.get_backfill_status(),
                "/api/stats": lambda: orch.get_stats(),
                "/api/gate-status": lambda: {
                    "signal_trade_count": orch.state.signal_trade_count,
                    "exploration_trade_count": orch.state.tier_2_total_trades,
                    "paper_exploration_trade_count": orch.state.paper_exploration_total_trades,
                    "refusal_counts": dict(orch.state.refusal_counts or {}),
                    "latest_refusals": dict(orch.state.latest_refusals or {}),
                    "shadow_forward": orch.get_shadow_status(),
                    "consecutive_ref_price_failures": orch.state.consecutive_ref_price_failures,
                    "news_fetch_degraded": orch.state.news_fetch_degraded,
                    "categories_attempted": orch.state.news_categories_attempted,
                    "categories_failed": orch.state.news_categories_failed,
                    "headlines_retrieved": orch.state.news_headlines_retrieved,
                    "articles_retrieved_total": orch.state.news_articles_retrieved_total,
                    "headlines_used": orch.state.news_headlines_used,
                    "status": "tracking signal trades only (Tier 2 excluded)",
                },
                "/api/shadow-forward": lambda: orch.get_shadow_status(),
                "/health": lambda: {"status": "ok", "timestamp": time.time()},
            },
            "POST": {
                "/api/mode": lambda p: orch.set_mode(
                    p.get("mode", "manual"),
                ),
                "/api/evaluate": lambda p: orch.evaluate_now(p.get("symbol", "SPY")),
                "/api/execute": lambda p: orch.execute_signal(
                    p.get("symbol", "SPY"),
                    p.get("conviction", 0.0),
                ),
                "/api/reset": lambda p: orch.reset(),
                "/api/kill": lambda p: orch.kill(),
                "/api/backfill/trigger": lambda p: orch._run_historical_backfill(),
            },
        }

        return routes.get(method, {}).get(path)

    def _send_json(self, status: int, data: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        # Authorization must be allowed or a cross-origin preflight rejects
        # every authenticated request before it is sent. The dashboard now
        # calls this server-to-server (no CORS involved), but anything that
        # does call from a browser would otherwise fail at preflight with no
        # useful error.
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def log_message(self, fmt, *args):
        logger.debug("API: %s", fmt % args)

    # Quiet favicon
    def do_OPTIONS(self):
        self._send_json(200, {})


def _authorized(handler) -> bool:
    """True only if the request carries the configured bearer token.

    This used to read `if API_AUTH_TOKEN:` -- auth applied only when a token
    happened to be configured, so an empty token disabled authentication
    entirely rather than refusing to serve. Combined with a 0.0.0.0 bind and
    a `--api-only` path that skipped the startup token check, `POST /api/mode`
    would flip the bot to autonomous for anyone who could reach the port.
    Verified by hand before the fix: it returned 200 and switched modes.

    So: no token configured means no request is authorized, ever.
    """
    if not API_AUTH_TOKEN:
        return False
    presented = handler.headers.get("Authorization", "")
    # Constant time, so the comparison does not leak the token by timing.
    return hmac.compare_digest(presented, "Bearer %s" % API_AUTH_TOKEN)


_PROCESS_LOCK_HANDLE = None


def acquire_process_lock(data_dir: str):
    """Refuse a second bot process for this environment.

    ``flock`` is released automatically by the kernel when the process exits,
    so a crashed process cannot leave a stale lock that needs manual cleanup.
    The lock is scoped to the resolved environment data directory: paper and
    live processes may coexist, but two processes for the same environment may
    not.
    """
    global _PROCESS_LOCK_HANDLE
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "bot.process.lock")
    handle = open(path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        handle.close()
        if getattr(exc, "errno", None) in (errno.EACCES, errno.EAGAIN) or isinstance(exc, BlockingIOError):
            raise RuntimeError(
                "another Educated Trades process already owns %s" % path)
        raise

    handle.seek(0)
    handle.truncate()
    handle.write("pid=%d\n" % os.getpid())
    handle.flush()
    os.fsync(handle.fileno())
    _PROCESS_LOCK_HANDLE = handle
    return handle


def release_process_lock() -> None:
    """Release the current process lock during graceful shutdown."""
    global _PROCESS_LOCK_HANDLE
    if _PROCESS_LOCK_HANDLE is not None:
        fcntl.flock(_PROCESS_LOCK_HANDLE.fileno(), fcntl.LOCK_UN)
        _PROCESS_LOCK_HANDLE.close()
        _PROCESS_LOCK_HANDLE = None


def create_api_server(host: str, port: int, orchestrator: 'Orchestrator'):
    """Bind and return the control API server.

    Binding is deliberately separate from ``serve_forever`` so startup can
    fail synchronously when another process already owns the port.  The bot
    must never continue its trading loops without its authenticated control
    API available.
    """
    APIHandler.orchestrator_ref = orchestrator
    server = ThreadingHTTPServer((host, port), APIHandler)
    # Do not let an in-flight request keep the process alive on shutdown.
    server.daemon_threads = True
    return server


def run_api_server(host: str, port: int, orchestrator: 'Orchestrator'):
    """Run the HTTP API server in a background thread.

    ThreadingHTTPServer, not HTTPServer. The plain server handles one request
    at a time, so a single slow endpoint -- a heavy query, a broker call that
    hangs on its 30-second timeout -- blocks every other route behind it,
    including POST /api/kill and POST /api/mode. Measured: one 3-second
    request delayed the kill switch by 2.6 seconds. The moment you most need
    the kill switch is the moment something else is already stuck.

    This is only safe because the stores underneath are now thread-safe:
    per-thread SQLite connections, the entry and exit claims, and atomic
    status-file writes. Threading the server before that would have turned an
    availability problem into a data-loss one.
    """
    server = create_api_server(host, port, orchestrator)
    orchestrator._api_server = server
    logger.info("API server listening on http://%s:%d", host, port)
    server.serve_forever()


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
class Orchestrator:
    """
    Main orchestrator that ties all backend modules together.

    Runs a periodic pipeline:
      News → Sentiment → Pattern → Trade Execution (if autonomous)
    """

    def __init__(
        self,
        poll_interval: int = DEFAULT_POLL_INTERVAL_S,
        min_conviction: float = MIN_CONVICTION_THRESHOLD,
        high_conviction: float = HIGH_CONVICTION_THRESHOLD,
        simulate: bool = True,
        allow_extended_hours: bool = False,
    ):
        # Polling much faster than the bar cadence just re-reads the same
        # closed bar; much slower and signals are acted on late.
        if not USE_DAILY_BARS:
            poll_interval = min(poll_interval,
                                max(30, BAR_TIMEFRAME_MINUTES * 60 // 2))
        self.poll_interval = poll_interval
        self.min_conviction = min_conviction
        self.high_conviction = high_conviction
        self.simulate = simulate
        self.allow_extended_hours = allow_extended_hours

        self.state = PipelineState()
        self.state.mode = OrchestratorMode.MANUAL
        # Override with persisted mode if it exists (survives restarts)
        self._load_persisted_mode()
        self._load_daily_tracking()

        # Engines (lazy init)
        self._sentiment_engine = None
        self._pattern_engine = None
        self._trading_engine = None
        self._news_ingestion = None
        self._stats_engine = None
        self._shadow_forward_store = None

        # Threading
        self._pipeline_thread: Optional[threading.Thread] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._api_thread: Optional[threading.Thread] = None
        self._api_server = None

        logger.info(
            "Orchestrator initialised (mode=%s, simulate=%s, "
            "poll=%ds, min_conv=%.2f, high_conv=%.2f)",
            self.state.mode.value, simulate, poll_interval,
            min_conviction, high_conviction,
        )

    # ------------------------------------------------------------------
    # Engine accessors (lazy init)
    # ------------------------------------------------------------------
    @property
    def sentiment(self):
        if self._sentiment_engine is None:
            mod = _import_sentiment()
            self._sentiment_engine = mod.MarketSentimentEngine()
        return self._sentiment_engine

    @property
    def patterns(self):
        if self._pattern_engine is None:
            mod = _import_patterns()
            self._pattern_engine = mod.PatternEngine()
        return self._pattern_engine

    @property
    def trading(self):
        if self._trading_engine is None:
            mod = _import_trading()
            self._trading_engine = mod.TradingEngine(simulate=self.simulate)
        return self._trading_engine

    @property
    def news(self):
        if self._news_ingestion is None:
            mod = _import_news()
            self._news_ingestion = mod.NewsIngestion()
        return self._news_ingestion

    @property
    def shadow(self):
        if self._shadow_forward_store is None:
            mod = _import_shadow_forward()
            self._shadow_forward_store = mod.ShadowForwardStore(
                SHADOW_DB_PATH,
                stop_pct=STOP_LOSS_PCT,
                target_pct=TAKE_PROFIT_PCT,
            )
        return self._shadow_forward_store

    @property
    def alerts(self):
        if not hasattr(self, '_alert_manager') or self._alert_manager is None:
            mod = _import_monitoring()
            self._alert_manager = mod.AlertManager()
        return self._alert_manager

    @property
    def audit(self):
        """Shared decision audit logger (writes data/audit_log.jsonl)."""
        if not hasattr(self, '_audit_logger') or self._audit_logger is None:
            mod = _import_monitoring()
            self._audit_logger = mod.DecisionAuditLogger.instance()
        return self._audit_logger

    def _save_daily_tracking(self) -> None:
        """Persist the daily loss-limit baseline to disk.

        Without this the daily loss limit is bypassable by a restart: the
        in-memory baseline resets to the current (already reduced) equity and
        the halt flag clears, handing the bot a fresh loss budget. With
        Restart=always in the systemd unit, that can repeat indefinitely.
        """
        try:
            os.makedirs(os.path.dirname(DAILY_STATE_FILE) or ".", exist_ok=True)
            payload = {
                "daily_start_date": self.state.daily_start_date,
                "daily_starting_equity": self.state.daily_starting_equity,
                "daily_loss_hit": bool(self.state.daily_loss_hit),
                "daily_pnl_pct": self.state.daily_pnl_pct,
                "saved_at": time.time(),
            }
            tmp = DAILY_STATE_FILE + ".tmp"
            with open(tmp, "w") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, DAILY_STATE_FILE)
        except Exception as exc:
            logger.error("Could not persist daily risk state: %s", exc)

    def _load_daily_tracking(self) -> None:
        """Restore the daily loss-limit baseline. Fails closed when unreadable.

        A daily state file we cannot read is not the same as no state; it may
        be hiding a breached limit, so trading halts until a new day.
        """
        if not os.path.exists(DAILY_STATE_FILE):
            return
        try:
            with open(DAILY_STATE_FILE) as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("daily risk state is not an object")
        except Exception as exc:
            self.state.daily_loss_hit = True
            self.state.mode = OrchestratorMode.DAILY_LOSS_LIMIT
            logger.critical(
                "Daily risk state unreadable (%s) — halting for the day rather "
                "than resuming with an unknown loss budget.", exc,
            )
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if data.get("daily_start_date") != today:
            logger.info("Daily risk state is from a previous day; starting fresh.")
            return

        self.state.daily_start_date = data.get("daily_start_date")
        equity = data.get("daily_starting_equity")
        self.state.daily_starting_equity = float(equity) if equity else None
        self.state.daily_pnl_pct = float(data.get("daily_pnl_pct") or 0.0)
        self.state.daily_loss_hit = bool(data.get("daily_loss_hit"))
        if self.state.daily_loss_hit:
            self.state.mode = OrchestratorMode.DAILY_LOSS_LIMIT
            logger.critical(
                "Daily loss limit was already breached today (%.2f%%) — "
                "remaining halted after restart.", self.state.daily_pnl_pct,
            )
        else:
            logger.info(
                "Daily risk state restored: baseline equity=$%.2f, pnl=%.2f%%",
                self.state.daily_starting_equity or 0.0, self.state.daily_pnl_pct,
            )

    def _load_persisted_mode(self) -> None:
        """Restore the operator mode and kill state from disk on startup.

        Precedence: killed_state file (highest) > operator mode file > default.
        """
        # 1. Check kill-state file first — if it exists, killed=True regardless
        if os.path.exists(KILLED_STATE_FILE):
            self.state.killed = True
            logger.warning(
                "KILLED state file found — killed flag set. "
                "Trading blocked until RESET_DRAWDOWN clears it."
            )
        else:
            self.state.killed = False

        # 2. Read operator mode file
        try:
            if os.path.exists(MODE_FILE):
                with open(MODE_FILE) as f:
                    saved = f.read().strip().lower()
                mode_map = {
                    "manual": OrchestratorMode.MANUAL,
                    "autonomous": OrchestratorMode.AUTONOMOUS,
                    "stopped": OrchestratorMode.STOPPED,
                    "killed": OrchestratorMode.MANUAL,  # legacy compatibility
                }
                if saved in mode_map:
                    self.state.mode = mode_map[saved]
                    logger.info("Restored operator mode from file: %s", saved)
                else:
                    self.state.mode = OrchestratorMode.MANUAL
                    logger.warning(
                        "Unknown mode '%s' in operator file — defaulting to MANUAL", saved
                    )
            else:
                self.state.mode = OrchestratorMode.MANUAL
                logger.info("No operator mode file — defaulting to MANUAL")
        except Exception as e:
            logger.warning("Could not load operator mode file: %s", e)

    def _save_persisted_mode(self) -> None:
        """Persist the operator's mode. Called ONLY from set_mode().

        The mode file is operator-owned, and this is the operator's own
        action arriving through the API -- so writing it here is the intended
        behaviour, not a violation of that ownership. Automated demotions
        (kill switch, daily loss limit, health failure) must NEVER call this:
        they set state flags and write KILLED_STATE_FILE, so the file keeps
        the pre-halt mode and the bot can resume it on the next day rollover.

        This was a `pass` stub whose docstring asserted the bot never writes
        the file, while permitting exactly this call in the same sentence.
        The effect was that POST /api/mode changed the mode in memory only,
        and every restart silently reverted to MANUAL -- including systemd's
        Restart=always after a crash. The bot would come back, pass preflight,
        cycle normally, and never trade again, announcing nothing.

        Observed in production: autonomous set at 09:45, process restarted at
        10:30, then a full afternoon of cycles in MANUAL with an empty
        decision journal. It presented exactly like a broken signal path.
        docs/MODE_PRECEDENCE.md had documented this as working throughout.

        Written atomically: a torn mode file read at startup is a bot whose
        operating state is decided by a partial write.
        """
        try:
            os.makedirs(os.path.dirname(MODE_FILE) or ".", exist_ok=True)
            tmp = "%s.tmp.%d" % (MODE_FILE, os.getpid())
            with open(tmp, "w") as handle:
                handle.write(self.state.mode.value + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, MODE_FILE)
            logger.info("Operator mode persisted: %s", self.state.mode.value)
        except OSError as exc:
            # Never let this break the mode change itself -- the in-memory
            # switch has already happened and the operator asked for it.
            logger.error(
                "Could not persist operator mode to %s: %s. The mode is "
                "active now but will NOT survive a restart.", MODE_FILE, exc)

    @property
    def clock(self):
        """US market-hours clock (ET). Gates trade execution to RTH."""
        if not hasattr(self, '_market_clock') or self._market_clock is None:
            import market_clock
            self._market_clock = market_clock.MarketClock(
                allow_extended_hours=self.allow_extended_hours
            )
        return self._market_clock

    @property
    def stats(self):
        """Portfolio-level stats computed from the completed trades log."""
        if self._stats_engine is None:
            mod = _import_stats()
            self._stats_engine = mod.PortfolioStats()
        return self._stats_engine


    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def preflight(self) -> dict:
        """Verify the system is safe to trade. Runs automatically at startup.

        A bot that needs a human to remember a checklist is not automatic --
        and the steps get skipped at exactly the wrong moment, on the day the
        credentials change. So every check that would otherwise be a line in a
        runbook lives here, and anything unsafe refuses to trade rather than
        warning into a log nobody reads.

        Returns {"ok": bool, "blocking": [...], "warnings": [...]}.
        """
        blocking, warnings, info = [], [], {}

        # --- environment and data segregation ---------------------------
        try:
            environment = self.trading.broker.environment
        except Exception:
            environment = "unknown"
        info["environment"] = environment
        info["data_dir"] = DATA_DIR
        info["data_dir_explicit"] = DATA_DIR_IS_EXPLICIT
        if DATA_DIR_IS_EXPLICIT:
            # An explicit DATA_DIR (typically left in the systemd
            # EnvironmentFile) overrides the credential-derived path, so paper
            # and live write to the same directory and the operator is back to
            # remembering to change it on the day they go live -- the exact
            # step this is built to remove.
            warnings.append(
                "DATA_DIR is set explicitly to %r, so paper and live share it. "
                "Remove DATA_DIR from the environment file and set DATA_ROOT "
                "instead; the paper/live split then follows the credentials "
                "with nothing to remember." % DATA_DIR)
        if environment == "live":
            logger.critical("PREFLIGHT: LIVE TRADING — orders use real money.")
            if os.path.basename(os.path.normpath(DATA_DIR)) != "live":
                blocking.append(
                    "live credentials but the data directory is %r — paper and "
                    "live must not share a ledger, journal or pattern store"
                    % DATA_DIR)

        # --- config coherence -------------------------------------------
        if INDICATOR_FETCH_BARS < INDICATOR_REQUIRED_BARS:
            blocking.append(
                "INDICATOR_FETCH_BARS (%d) is below INDICATOR_REQUIRED_BARS "
                "(%d): indicators would silently return None and no signal "
                "would ever fire"
                % (INDICATOR_FETCH_BARS, INDICATOR_REQUIRED_BARS))
        try:
            from trading import (DEFAULT_MAX_TOTAL_EXPOSURE,
                                 DEFAULT_MAX_POSITION_SIZE)
            if DEFAULT_MAX_TOTAL_EXPOSURE < DEFAULT_MAX_POSITION_SIZE:
                warnings.append(
                    "total exposure cap (%.0f%%) is below the per-position cap "
                    "(%.0f%%) — the per-position cap can never be reached"
                    % (DEFAULT_MAX_TOTAL_EXPOSURE * 100,
                       DEFAULT_MAX_POSITION_SIZE * 100))
        except Exception:
            pass
        if TAKE_PROFIT_PCT <= STOP_LOSS_PCT:
            warnings.append(
                "take profit (%.4f%%) is not above the stop (%.4f%%): the "
                "trade needs a win rate above 50%% just to break even"
                % (TAKE_PROFIT_PCT * 100, STOP_LOSS_PCT * 100))
        if not TRADING_SYMBOLS:
            blocking.append("TRADING_SYMBOLS is empty — nothing to trade")

        # --- account can actually trade the universe --------------------
        try:
            account = self.trading.broker.get_account()
            equity = float(account.equity)
            info["equity"] = equity
            from trading import DEFAULT_MAX_POSITION_SIZE
            untradeable = []
            for symbol in TRADING_SYMBOLS:
                try:
                    price = self.trading._get_reference_price(symbol)
                except Exception:
                    continue
                if price and int(equity * DEFAULT_MAX_POSITION_SIZE / price) < 1:
                    untradeable.append("%s ($%.0f)" % (symbol, price))
            if untradeable:
                message = (
                    "one share exceeds the per-position cap for: %s — these "
                    "will size to zero and never trade on $%.0f of equity"
                    % (", ".join(untradeable), equity))
                (blocking if len(untradeable) == len(TRADING_SYMBOLS)
                 else warnings).append(message)
        except Exception as exc:
            warnings.append("could not verify account sizing: %s" % exc)

        # --- learning state produced by superseded logic -----------------
        try:
            conn = self.patterns.db._connect()
            row = conn.execute(
                "SELECT COUNT(*) c FROM pattern_memory "
                "WHERE outcome IN ('win','loss') AND side IS NULL").fetchone()
            unsided = row["c"] if row else 0
            if unsided:
                info["unsided_outcomes"] = unsided
                blocking.append(
                    "%d resolved trades have no recorded side. These predate "
                    "the direction-aware P&L fix, so every short among them "
                    "was learned with the sign inverted. Run "
                    "scripts/reset_learning.py --apply before trading."
                    % unsided)
        except Exception:
            pass  # fresh database, or schema not yet created

        # --- the kill switch --------------------------------------------
        try:
            truth = self.trading.position_truth
            if truth is not None and truth.safety.kill_engaged():
                blocking.append(
                    "the execution kill switch is engaged — release it "
                    "deliberately before trading")
        except Exception as exc:
            blocking.append("could not read the kill switch: %s" % exc)

        ok = not blocking
        for message in blocking:
            logger.critical("PREFLIGHT BLOCKED: %s", message)
        for message in warnings:
            logger.warning("PREFLIGHT: %s", message)
        if ok:
            logger.info(
                "PREFLIGHT PASSED (environment=%s, data=%s, symbols=%s)",
                environment, DATA_DIR, ",".join(TRADING_SYMBOLS))
        self.state.preflight = {"ok": ok, "blocking": blocking,
                                "warnings": warnings, "info": info}
        return self.state.preflight

    def start(self) -> None:
        """Start the orchestrator pipeline and API server."""
        if self.state.running:
            logger.warning("Orchestrator already running")
            return

        self.state.running = True
        self._stop_event.clear()

        # ---- Preflight: refuse to trade rather than warn into a log ----
        report = self.preflight()
        if not report["ok"]:
            self.state.mode = OrchestratorMode.MANUAL
            self.state.errors.append(
                "preflight failed: %s" % "; ".join(report["blocking"]))
            logger.critical(
                "Preflight failed — starting in MANUAL mode. No orders will "
                "be placed until the blocking issues above are resolved.")

        # ---- Mandatory auth check ----
        api_token = os.environ.get("API_AUTH_TOKEN", "")
        if not api_token:
            logger.critical(
                "API_AUTH_TOKEN environment variable is required -- "
                "set a strong token and restart"
            )
            sys.exit(1)

                # ---- Ensure DATA_DIR exists ----
        os.makedirs(DATA_DIR, exist_ok=True)
        import patterns as pmod
        logger.info("DATA_DIR=%s  DB_PATH=%s  kill=%s  heartbeat=%s",
            DATA_DIR,
            pmod.DB_PATH,
            os.path.join(DATA_DIR, "KILL_SWITCH"),
            os.path.join(DATA_DIR, "heartbeat"),
        )

        # ---- Startup Recovery: reconcile position state with broker ----
        self._recover_positions_on_startup()

        # ---- Startup Recovery: restore drawdown state from alerts DB ----
        try:
            from alert_db import get_latest_alert_by_type
            dd_alert = get_latest_alert_by_type("DRAWDOWN_STATE")

            if dd_alert is None:
                # CASE 1: No DRAWDOWN_STATE alert (fresh DB) -- fresh baseline
                try:
                    acct = self.trading.broker.get_account()
                    fresh_equity = float(acct.equity) if hasattr(acct, 'equity') else 0.0
                except Exception:
                    fresh_equity = 0.0
                    self.state.peak_equity = 0.0
                    self.state.max_drawdown_pct = 0.0
                    self.state.position_size_multiplier = 1.0
                    self.state.drawdown_killed = True
                    logger.critical(
                        "Drawdown state: fresh baseline failed -- could not read account equity. "
                        "Defaulting to drawdown_killed=True. Trading is blocked."
                    )
                else:
                    self.state.peak_equity = fresh_equity
                    self.state.max_drawdown_pct = 0.0
                    self.state.position_size_multiplier = 1.0
                    self.state.drawdown_killed = False
                    logger.info(
                        "Drawdown state: no prior state found -- fresh baseline set "
                        "(peak=%.2f, drawdown_killed=False)", fresh_equity,
                    )
            else:
                # Alert exists -- parse with json.loads
                msg = dd_alert.get("message", "")
                try:
                    data = json.loads(msg)
                except Exception:
                    data = None

                if data is None or not isinstance(data, dict):
                    # CASE 2: Alert found but unreadable -- kill trading
                    self.state.peak_equity = 0.0
                    self.state.max_drawdown_pct = 0.0
                    self.state.position_size_multiplier = 1.0
                    self.state.drawdown_killed = True
                    logger.critical(
                        "Drawdown state: found alert but failed to parse -- "
                        "defaulting to drawdown_killed=True. Trading is blocked."
                    )
                else:
                    core_keys = ["peak_equity", "max_drawdown_pct",
                                 "position_size_multiplier", "drawdown_killed", "killed"]
                    if all(k in data for k in core_keys):
                        # CASE 3a: Core keys present -- restore
                        self.state.peak_equity = float(data["peak_equity"])
                        self.state.max_drawdown_pct = float(data["max_drawdown_pct"])
                        self.state.position_size_multiplier = float(data["position_size_multiplier"])
                        self.state.drawdown_killed = bool(data["drawdown_killed"])
                        self.state.killed = bool(data.get("killed", False))

                        # Migration: tier_2/signal keys added in PR #17.
                        # Persisted state from before that merge won't have them.
                        migration_keys = [
                            "tier_2_trades_today", "tier_2_total_trades",
                            "tier_2_eval_cycle", "signal_trade_count",
                            "paper_exploration_trades_today",
                            "paper_exploration_total_trades",
                        ]
                        missing_migration = [k for k in migration_keys if k not in data]
                        if missing_migration:
                            logger.warning(
                                "Drawdown state: backfilling %d missing keys (%s) to 0 "
                                "(state predates PR #17 schema)",
                                len(missing_migration), ", ".join(missing_migration),
                            )
                        self.state.tier_2_trades_today = int(data.get("tier_2_trades_today", 0))
                        self.state.tier_2_total_trades = int(data.get("tier_2_total_trades", 0))
                        self.state.tier_2_eval_cycle = int(data.get("tier_2_eval_cycle", 0))
                        self.state.signal_trade_count = int(data.get("signal_trade_count", 0))
                        self.state.paper_exploration_trades_today = int(
                            data.get("paper_exploration_trades_today", 0))
                        self.state.paper_exploration_total_trades = int(
                            data.get("paper_exploration_total_trades", 0))

                        # If we backfilled missing keys, persist them immediately
                        # so the next restart loads a clean state without a migration warning.
                        if missing_migration:
                            try:
                                from alert_db import insert_alert
                                state = self._build_drawdown_state_dict()
                                insert_alert("DRAWDOWN_STATE", json.dumps(state), "info")
                                logger.info("Persisted backfilled drawdown state — next restart will load clean")
                            except Exception as e:
                                logger.error("Failed to persist backfilled drawdown state: %s", e, exc_info=True)

                        logger.info(
                            "Drawdown state restored: peak=%.2f max_dd=%.2f%% "
                            "mult=%.2f killed=%s",
                            self.state.peak_equity, self.state.max_drawdown_pct,
                            self.state.position_size_multiplier, self.state.drawdown_killed,
                        )
                        if self.state.drawdown_killed:
                            logger.critical(
                                "DRAWDOWN KILL ACTIVE: peak_equity=%.2f "
                                "max_dd=%.2f%% -- Trading is blocked "
                                "until manually reset.",
                                self.state.peak_equity, self.state.max_drawdown_pct,
                            )
                    else:
                        # CASE 3b: Core keys missing -- kill trading
                        missing = [k for k in core_keys if k not in data]
                        self.state.peak_equity = 0.0
                        self.state.max_drawdown_pct = 0.0
                        self.state.position_size_multiplier = 1.0
                        self.state.drawdown_killed = True
                        logger.critical(
                            "Drawdown state: alert has missing core keys (%s) -- "
                            "defaulting to drawdown_killed=True. Trading is blocked.",
                            ", ".join(missing),
                        )
        except Exception as dd_e:
            # Any unexpected error -- fail safe: kill trading
            self.state.peak_equity = 0.0
            self.state.max_drawdown_pct = 0.0
            self.state.position_size_multiplier = 1.0
            self.state.drawdown_killed = True
            logger.critical(
                "Drawdown state recovery FAILED (%s) -- "
                "defaulting to drawdown_killed=True. Trading is blocked.", dd_e,
            )

        # ---- Startup backfill: fetch 200 daily bars for each symbol ----
        # Only runs once per process start (guarded by backfill_done flag).
        if self.state.backfill_done:
            logger.info("Backfill already completed this session -- skipping")
        else:
            try:
                need_backfill = False
                for sym in TRADING_SYMBOLS:
                    bars = self.patterns.db.get_recent_daily_bars(sym, limit=200)
                    if len(bars) < 200:
                        logger.info("Backfill needed for %s: %d bars (<200)", sym, len(bars))
                        need_backfill = True
                        break
                    else:
                        logger.info("Backfill not needed for %s: %d bars", sym, len(bars))
                if need_backfill:
                    bf_result = self._run_historical_backfill()
                    logger.info("Startup backfill result: %s", bf_result.get("status", "unknown"))
                else:
                    logger.info("Backfill skipped -- all symbols have >=200 daily bars")
                self.state.backfill_done = True
            except Exception as sbe:
                logger.warning("Startup backfill FAILED: %s", sbe)

        # ---- Pattern memory starts empty — builds from real pipeline outcomes ----

        # Bind the control API before starting any worker loop.  If the port
        # is already occupied, this raises synchronously and startup aborts;
        # continuing without the authenticated control API would be unsafe.
        self._api_server = create_api_server(API_BIND, API_PORT, self)
        logger.info("API server bound on http://%s:%d", API_BIND, API_PORT)

        # Start API server in background thread
        self._api_thread = threading.Thread(
            target=self._api_server.serve_forever,
            daemon=True,
        )
        self._api_thread.start()

        # Record startup milestone
        try:
            self.patterns.db.record_milestone(
                milestone_type="startup",
                value=0,
                note=f"Orchestrator started (mode={self.state.mode.value})",
            )
        except Exception:
            pass

        # Start pipeline in background thread
        self._pipeline_thread = threading.Thread(
            target=self._pipeline_loop,
            daemon=True,
        )
        self._pipeline_thread.start()

        # Start the dedicated fast-track position monitor so stop-loss /
        # take-profit exits are enforced every POSITION_MONITOR_INTERVAL_S
        # rather than only once per (slow) pipeline cycle.
        self._monitor_thread = threading.Thread(
            target=self._position_monitor_loop,
            daemon=True,
        )
        self._monitor_thread.start()

        # Start the external heartbeat watchdog (checks every 60s, alerts
        # Discord if heartbeat goes stale >5 min during market hours).
        try:
            import subprocess
            subprocess.Popen(
                [os.path.join(APP_ROOT, "watchdog_loop.sh")],
                start_new_session=True,
            )
        except Exception as wde:
            logger.warning("Watchdog loop failed to start: %s", wde)

        logger.info(
            "Orchestrator started (mode=%s, api=:%d, monitor=%ds)",
            self.state.mode.value, API_PORT, POSITION_MONITOR_INTERVAL_S,
        )

    def _write_killed_state_file(self) -> None:
        """Persist the killed flag to disk so it survives restarts."""
        try:
            with open(KILLED_STATE_FILE, "w") as f:
                f.write(f"killed at {time.time()}\n")
        except Exception as e:
            logger.error("Failed to write killed state file: %s", e)

    def _check_active_positions(self, context: str = "monitor") -> List[dict]:
        """
        Evaluate every open position against the hard stop-loss / take-profit
        thresholds and CLOSE any that have breached a level.

        Crucially, this flattens the position at the BROKER (a real closing
        order in live/paper mode) before recording the realised outcome in the
        database — so the broker and our books never diverge. Used both by the
        main pipeline (Step 0) and the dedicated fast-track monitor thread.

        Returns the list of close-result dicts (empty if nothing closed).
        """
        active_positions = self.patterns.db.get_active_positions()
        if not active_positions:
            return []

        closed_results: List[dict] = []
        for pos in active_positions:
            symbol = pos.get("symbol")
            record_id = pos.get("record_id")
            side = pos.get("side")

            # A single malformed row must never stop the others being
            # evaluated -- an unhandled error here silently disables the stop
            # loss for EVERY remaining position.
            try:
                entry_price = float(pos.get("entry_price") or 0.0)
            except (TypeError, ValueError):
                entry_price = 0.0
            if not (entry_price > 0 and math.isfinite(entry_price)):
                logger.critical(
                    "  [%s] %s (record %s) has an unusable entry price (%r) — "
                    "cannot evaluate its stop. Position left OPEN and "
                    "UNPROTECTED; needs manual attention.",
                    context, symbol, record_id, pos.get("entry_price"),
                )
                self._flag_unprotected(symbol, "invalid entry price")
                continue

            # Determine the current price.
            if self.trading.broker.is_simulating:
                import random
                movement = random.gauss(0, 0.001)
                current_price = round(entry_price * (1 + movement / 100.0), 2)
            else:
                try:
                    current_price = self.trading._get_reference_price(symbol)
                except Exception as price_error:
                    current_price = None
                    logger.warning(
                        "  [%s] Price lookup failed for %s: %s",
                        context, symbol, price_error,
                    )
                if not current_price or current_price <= 0:
                    # Previously this substituted the entry price, making
                    # profit_pct exactly 0 -- so no stop could ever fire during
                    # a data outage, silently and without a trace. Skip loudly
                    # instead, and escalate if it keeps happening.
                    self._flag_unprotected(symbol, "no live price")
                    continue

            self._clear_unprotected(symbol)

            # Track the path this trade takes so an exit can be judged against
            # what was actually available, not just its final number.
            track = self.state.excursions.setdefault(
                symbol, {"best": current_price, "worst": current_price,
                         "entry": entry_price, "side": side})
            track["best"] = max(track["best"], current_price) if side == "buy" \
                else min(track["best"], current_price)
            track["worst"] = min(track["worst"], current_price) if side == "buy" \
                else max(track["worst"], current_price)

            if side == "buy":
                profit_pct = (current_price - entry_price) / entry_price * 100.0
            else:
                profit_pct = (entry_price - current_price) / entry_price * 100.0

            # Hard stop-loss / take-profit enforcement (asymmetric: cut losers
            # at -2.5%, let winners run to the +3.0% target).
            hit_stop = profit_pct <= -STOP_LOSS_PCT * 100.0
            hit_target = profit_pct >= TAKE_PROFIT_PCT * 100.0
            if not (hit_stop or hit_target):
                continue

            trigger = "STOP_LOSS" if hit_stop else "TAKE_PROFIT"

            # 1. Flatten at the broker FIRST. In live/paper mode this submits a
            #    real closing order; in simulation it is a guaranteed no-op.
            broker_close = self.trading.close_position_guarded(symbol, reason=trigger)
            if not broker_close.success:
                logger.error(
                    "  ⚠️ [%s] Broker close FAILED for %s (%s): %s — "
                    "leaving position tracked for retry.",
                    context, symbol, trigger, broker_close.error,
                )
                # Do not record a DB close if the broker didn't actually
                # flatten — avoids phantom P&L / book divergence.
                continue

            # 2. Record the realised outcome in pattern memory + P&L milestone.
            close_result = self.patterns.close_tracked_position(
                record_id=record_id, current_price=current_price,
            )
            # Gate: only tier='signal' trades count toward the signal trade gate
            try:
                _tier_row = self.patterns.db._connect().execute(
                    "SELECT tier FROM pattern_memory WHERE id=?", (record_id,)
                ).fetchone()
                if _tier_row and _tier_row["tier"] == "signal":
                    self.state.signal_trade_count += 1
                    logger.debug("Gate: signal_trade_count incremented to %d (record_id=%d)",
                                 self.state.signal_trade_count, record_id)
            except Exception:
                pass
            close_result["trigger"] = trigger
            close_result["broker_order_id"] = broker_close.order_id
            try:
                from decision_log import DecisionLog
                _journal = DecisionLog()
                _path = self.state.excursions.pop(symbol, None)
                if _path:
                    _journal.excursion(
                        symbol, trade_id=str(broker_close.order_id or record_id),
                        entry_price=entry_price, best_price=_path["best"],
                        worst_price=_path["worst"], exit_price=current_price,
                        side=side,
                    )
                _journal.exited(
                    symbol, side, trigger=trigger,
                    entry_price=entry_price, exit_price=current_price,
                    profit_pct=round(profit_pct, 4),
                    hold_hours=float(close_result.get("hours_open", 0) or 0),
                    quantity=int(pos.get("quantity", 0) or 0),
                    trade_id=str(broker_close.order_id or record_id),
                    detail="context=%s" % context,
                )
            except Exception:
                pass
            closed_results.append(close_result)
            logger.info(
                "  📊 [%s] Position closed [%s] %s: %.2f%% "
                "(P&L=$%.2f, broker_order=%s)",
                context, symbol, trigger, profit_pct,
                close_result.get("dollar_pnl", 0),
                broker_close.order_id,
            )

        return closed_results


    def _engage_execution_kill(self, engaged: bool = True) -> None:
        """Mirror the orchestrator kill onto the ORDER ledger.

        Two independent kill switches is the same failure as two position
        authorities: halting one while the other still authorises orders means
        nothing is actually stopped. This keeps them in lockstep.
        """
        try:
            truth = self.trading.position_truth
            if truth is not None:
                truth.safety.set_kill(engaged)
                logger.critical("Execution ledger kill switch set to %s", engaged)
        except Exception as exc:
            logger.error("Could not set execution ledger kill switch: %s", exc)

    def carries_overnight_risk(self) -> bool:
        """Would a position opened now probably still be open at the close?

        Sized against gap risk if so. Fails CLOSED -- if the session end
        cannot be determined, assume overnight, because under-sizing costs
        opportunity while over-sizing costs money.
        """
        if USE_DAILY_BARS:
            return True
        try:
            status = self.clock.status()
        except Exception as exc:
            logger.warning(
                "Could not determine time to close (%s); assuming overnight risk.",
                exc)
            return True
        if not status.get("is_open"):
            return True
        seconds_left = status.get("seconds_to_close")
        if seconds_left is None:
            return True
        return seconds_left < OVERNIGHT_HORIZON_MINUTES * 60

    def _flag_unprotected(self, symbol: str, why: str) -> None:
        """Record that a position could not be evaluated against its stop.

        A position we cannot price is a position without a working software
        stop. That must be visible, and it must get louder the longer it
        lasts, rather than passing silently every cycle.
        """
        counts = self.state.unprotected
        counts[symbol] = counts.get(symbol, 0) + 1
        streak = counts[symbol]
        if streak in (1, 3) or streak % 10 == 0:
            level = logger.critical if streak >= 3 else logger.warning
            level(
                "Position %s UNPROTECTED for %d consecutive check(s): %s. "
                "Software stop-loss cannot evaluate; broker bracket (if any) "
                "is the only remaining protection.",
                symbol, streak, why,
            )
        if streak == 3:
            try:
                from alert_db import insert_alert
                insert_alert(
                    "UNPROTECTED_POSITION",
                    json.dumps({"symbol": symbol, "reason": why, "streak": streak}),
                    level="critical",
                )
            except Exception:
                pass

    def _clear_unprotected(self, symbol: str) -> None:
        counts = self.state.unprotected
        if counts and symbol in counts:
            logger.info("Position %s can be priced again; stop-loss active.", symbol)
            counts.pop(symbol, None)

    def unprotected_positions(self) -> dict:
        """Symbols whose stop-loss currently cannot be evaluated."""
        return dict(self.state.unprotected or {})

    def _check_daily_loss_limit(self) -> bool:
        """
        Evaluate the current daily P&L against the configured loss limit.

        If the daily loss exceeds DAILY_LOSS_LIMIT_PCT of starting equity:
          - Immediately close all open positions
          - Switch to DAILY_LOSS_LIMIT mode (paused for the day)
          - Log a CRITICAL alert

        Returns True if the limit was breached (trading halted for the day),
        False if trading may continue.
        """
        # First, make sure tracking is up to date
        self._update_daily_tracking()

        if self.state.daily_loss_hit:
            return True  # Already halted

        if self.state.daily_starting_equity is None or self.state.daily_starting_equity <= 1.0:
            return False  # Not enough data to evaluate (equity glitch or fresh state)

        # Check if the loss limit has been breached
        if self.state.daily_pnl_pct <= -DAILY_LOSS_LIMIT_PCT:
            self.state.daily_loss_hit = True
            # Persist BEFORE closing positions: if we die mid-flatten, the halt
            # must survive the restart.
            self._save_daily_tracking()

            # Close all open positions immediately
            closed = self._close_all_active_positions()

            # Pause trading for the day
            self.state.mode = OrchestratorMode.DAILY_LOSS_LIMIT

            logger.critical(
                "🔴 Daily loss limit breached: %.2f%% of starting equity "
                "($%.2f → $%.2f). Trading halted for the day. "
                "Closed %d positions.",
                self.state.daily_pnl_pct,
                self.state.daily_starting_equity,
                self.state.daily_starting_equity
                * (1 + self.state.daily_pnl_pct / 100),
                len(closed),
            )
            self.state.mode = OrchestratorMode.DAILY_LOSS_LIMIT
            self._save_daily_tracking()

            # Record milestone
            try:
                self.patterns.db.record_milestone(
                    milestone_type="daily_loss_limit",
                    value=self.state.daily_pnl_pct,
                    note=(
                        f"Daily loss limit breached: {self.state.daily_pnl_pct}%"
                        f" (limit: {DAILY_LOSS_LIMIT_PCT}%), closed {len(closed)} positions"
                    ),
                )
            except Exception:
                pass

            return True

        return False

    # ------------------------------------------------------------------
    # Market Regime Detection (ADX)
    # ------------------------------------------------------------------

    def _check_file_kill_switch(self) -> bool:
        """
        Check if the KILL_SWITCH file exists on disk.

        This is a belt-and-suspenders check alongside the API-based kill:
        the file can be written by an external process or the admin even
        if the API server is unresponsive.

        Returns True if the kill switch was just triggered by file.
        """
        if not os.path.exists(self.KILL_SWITCH_FILE):
            return False

        if self.state.killed:
            return True  # Already in killed state

        # File exists but we're not yet killed — trigger it
        logger.critical(
            "🔴 KILL_SWITCH file detected — triggering kill"
        )
        try:
            closed = self._close_all_active_positions()
            logger.critical("Kill (file): closed %d positions", len(closed))
        except Exception as e:
            logger.error("Kill (file): close positions failed: %s", e)

        self.state.killed = True
        self.state.errors.append("KILL_SWITCH FILE DETECTED")
        self._write_killed_state_file()
        logger.warning(
            "KILLED flag set (trigger: file kill switch) — persisted to %s",
            KILLED_STATE_FILE,
        )
        return True


    def _close_all_active_positions(self) -> List[dict]:
        """
        Force-close ALL actively tracked positions, regardless of P&L.
        Used when the daily loss limit is breached to flatten the book.

        Returns a list of close-result dicts (empty if nothing was open).
        """
        closed_results: List[dict] = []
        active_positions = self.patterns.db.get_active_positions()
        if not active_positions:
            return closed_results

        logger.warning(
            "Force-closing %d active positions due to daily loss limit breach",
            len(active_positions),
        )

        for pos in active_positions:
            symbol = pos["symbol"]
            record_id = pos["record_id"]
            try:
                # 1. Flatten at the broker
                broker_close = self.trading.close_position_guarded(
                    symbol, reason="DAILY_LOSS_LIMIT")
                if not broker_close.success:
                    logger.error(
                        "Force-close FAILED for %s: %s",
                        symbol, broker_close.error,
                    )
                    continue

                # 2. Record close in pattern memory
                close_result = self.patterns.close_tracked_position(
                    record_id=record_id, current_price=pos["entry_price"],
                )
                close_result["trigger"] = "DAILY_LOSS_LIMIT"
                close_result["broker_order_id"] = broker_close.order_id
                closed_results.append(close_result)

                logger.info(
                    "  🔒 Force-closed %s (record=%d, broker_order=%s)",
                    symbol, record_id, broker_close.order_id,
                )
            except Exception as e:
                logger.error("Force-close exception for %s: %s", symbol, e)

        return closed_results



    def _compute_indicators_this_cycle(self, market_open: bool) -> None:
        """Compute RSI/ADX/regime from fresh OHLC bars for all primary symbols.
        
        Must be called in the market-hours branch BEFORE Step 2.5.
        Stores results in self.state.live_indicators for downstream steps.
        Fails closed: if a symbol cannot be computed, don't trade it.
        """
        import traceback
        symbols = list(TRADING_SYMBOLS)
        pmod = _import_patterns()
        self.state.live_indicators = {}
        self.state.indicators_valid = False
        any_valid = False
        
        # Fail closed on clock failure: unknown phase with error = unsafe
        mh_phase = self.state.market_hours.get("phase", "")
        if mh_phase == "unknown" and "error" in self.state.market_hours:
            logger.warning(
                "Market-hours clock FAILED (error=%s) — refusing to compute indicators",
                self.state.market_hours.get("error", "unknown"),
            )
            return
        
        if not market_open:
            logger.info("Market closed — skipping indicator compute")
            return
        
        for sym in symbols:
            try:
                ohlc = self._fetch_ohlc(sym, bars=INDICATOR_FETCH_BARS)
                if ohlc is None:
                    logger.critical(
                        "INDICATOR [%s]: OHLC fetch returned None — no signal will be generated. "
                        "Check Alpaca credentials and data availability.", sym
                    )
                    continue
                highs, lows, closes = ohlc["highs"], ohlc["lows"], ohlc["closes"]
                rsi = pmod.compute_rsi(closes)
                adx = pmod.compute_adx(highs, lows, closes)
                regime = pmod.classify_regime(adx)
                # EMAs feed trend_conviction, which replaced sentiment as the
                # directional input. They must be computed here or the trend
                # path silently sees None and never generates a signal.
                ema_short = pmod.compute_ema(closes, EMA_SHORT_PERIOD)
                ema_long = pmod.compute_ema(closes, EMA_LONG_PERIOD)
                prev_ema_short = pmod.compute_ema(closes[:-1], EMA_SHORT_PERIOD)
                prev_ema_long = pmod.compute_ema(closes[:-1], EMA_LONG_PERIOD)
                # Recent volatility makes trend conviction dimensionless, so
                # the same thresholds hold at any bar size.
                volatility_pct = pmod.realized_volatility_pct(closes)
                # Only accept this symbol if BOTH RSI and ADX computed
                # successfully.  classify_regime(None) silently returns
                # "unknown", masking a zero-data ADX as a valid regime.
                if rsi is None or adx is None:
                    logger.warning(
                        "INDICATOR [%s]: partial data — RSI=%s ADX=%s — skipping symbol",
                        sym,
                        "ok" if rsi is not None else "FAILED",
                        "ok" if adx is not None else "FAILED",
                    )
                    continue
                self.state.live_indicators[sym] = {
                    "rsi": rsi,
                    "adx": adx,
                    "regime": regime,
                    "ema_short": ema_short,
                    "ema_long": ema_long,
                    "prev_ema_short": prev_ema_short,
                    "prev_ema_long": prev_ema_long,
                    "volatility_pct": volatility_pct,
                    "bar_timestamp": ohlc.get("bar_timestamp"),
                }
                # Advance already-recorded shadows from closed bars. This
                # store has no broker methods and is separate from learning.
                if hasattr(self, "_shadow_forward_store"):
                    try:
                        self.shadow.observe_bars(sym, ohlc)
                    except Exception as shadow_exc:
                        logger.warning(
                            "SHADOW [%s]: outcome update failed "
                            "(trading unaffected): %s", sym, shadow_exc,
                        )
                any_valid = True
                _bar_ts = ohlc.get("bar_timestamp") if ohlc else None
                logger.info(
                    "INDICATOR [%s]: RSI=%.2f ADX=%.2f regime=%s%s",
                    sym, rsi if rsi else float("nan"), adx if adx else float("nan"),
                    regime,
                    f" (bars through {_bar_ts})" if _bar_ts else "",
                )
            except Exception as e:
                logger.error("INDICATOR [%s]: compute FAILED — %s\n%s", sym, e, traceback.format_exc())
        
        if any_valid:
            self.state.indicators_valid = True
        else:
            logger.critical(
                "INDICATOR: ALL symbols failed — indicators_valid=False. "
                "No trades will be generated this cycle."
            )
        


    def _detect_market_regime(self, symbols: Optional[List[str]] = None) -> dict:
        """
        Compute the 14-period ADX for the given symbols (default SPY/QQQ) and
        derive the current trading regime + how it should modulate execution:

          - trending      (avg ADX > 25): trend-following, full size
          - range_bound   (avg ADX < 20): MEAN-REVERSION (fade RSI extremes)
          - transitioning (20-25):        trend-following, HALF size
          - unknown       (no data):      no strategy, zero size (fail closed)
        """
        pmod = _import_patterns()
        symbols = symbols or ["SPY", "QQQ"]
        indicators = {}
        for sym in symbols:
            ohlc = self._fetch_ohlc(sym)
            adx = None
            if ohlc:
                adx = pmod.compute_adx(ohlc["highs"], ohlc["lows"], ohlc["closes"])
            indicators[sym] = {"adx": adx}

        regime_info = regime_info_from_indicators(indicators)
        self.state.market_regime = regime_info
        logger.info(
            "Regime: %s (avg ADX=%s) → strategy=%s, size×%.2f",
            regime_info["regime"], regime_info["adx"],
            regime_info["strategy"], regime_info["position_size_factor"],
        )
        return regime_info


    def _fetch_ohlc(self, symbol: str,
                    bars: int = INDICATOR_FETCH_BARS) -> Optional[dict]:
        """Fetch bars. The DEFAULT matters: it decides what the bot trades on.

        This defaulted to 75. Four call sites use it, and only the one that
        produces the visible INDICATOR log lines passed an explicit
        INDICATOR_FETCH_BARS. The signal path -- `_run_pipeline_cycle`,
        which computes the EMA-20/EMA-50 pair that decides trend direction --
        took the default and therefore computed EMAs from 75 bars.

        Measured on realistic price series, EMA-50 from 75 bars disagrees with
        EMA-50 from 200 bars by up to 0.35%, which is half the 0.693% stop
        distance. The magnitude is not the real problem: trend direction is
        decided by whether EMA-20 is above or below EMA-50, and an error that
        size can FLIP that comparison. The bot would read an uptrend as a
        downtrend while the log showed the correct, 200-bar values.

        This is the same defect fixed earlier in the indicator path. Fixing
        one call site and leaving the default wrong meant the numbers being
        logged and the numbers being traded on came from different histories.
        The default is now the correct value, so a new call site inherits
        correctness instead of inheriting the bug.

        Live/paper: pulls real bars from Alpaca's market-data API.
        Returns {"highs":[...],"lows":[...],"closes":[...]} or None on failure.
        """
        if not self.trading.broker.is_simulating:
            from alpaca.data.enums import DataFeed

            key = os.environ.get("APCA_API_KEY_ID", "")
            secret = os.environ.get("APCA_API_SECRET_KEY", "")
            logger.info("OHLC fetch for %s — key_present=%s, bars=%d", symbol, bool(key), bars)
            if not key or not secret:
                logger.error("OHLC fetch for %s — APCA_API_KEY_ID or APCA_API_SECRET_KEY missing", symbol)
                return None
            try:
                from alpaca.data.historical import StockHistoricalDataClient
                from alpaca.data.requests import StockBarsRequest
                from alpaca.data.timeframe import TimeFrame
                from datetime import datetime, timedelta

                client = StockHistoricalDataClient(key, secret)
                if USE_DAILY_BARS:
                    timeframe = TimeFrame.Day
                    # Calendar days needed for `bars` sessions, allowing for
                    # weekends and holidays.
                    start = datetime.now(timezone.utc) - timedelta(
                        days=bars * 2 + 10)
                else:
                    from alpaca.data.timeframe import TimeFrameUnit
                    timeframe = TimeFrame(BAR_TIMEFRAME_MINUTES,
                                          TimeFrameUnit.Minute)
                    # Only ~390 minutes of regular session per day, so the
                    # window has to be computed in SESSIONS, not in bars.
                    # Using the daily arithmetic here would ask for minutes
                    # and receive a few hours of data.
                    bars_per_session = max(
                        1.0, 390.0 / max(1, BAR_TIMEFRAME_MINUTES))
                    sessions_needed = bars / bars_per_session
                    start = datetime.now(timezone.utc) - timedelta(
                        days=math.ceil(sessions_needed * 1.6) + 5)
                req = StockBarsRequest(
                    symbol_or_symbols=[symbol],
                    timeframe=timeframe,
                    start=start,
                    feed=DataFeed.IEX,  # Paper accounts only get IEX data
                )
                resp = client.get_stock_bars(req)
                data = resp.data.get(symbol, []) if hasattr(resp, "data") else []
                logger.info(
                    "OHLC fetch for %s — got %d bars from Alpaca (resp type=%s)",
                    symbol, len(data), type(resp).__name__,
                )
                # Drop a bar that is still forming. Polling every 2 minutes
                # on a 30-minute bar means reading the SAME incomplete bar 15
                # times, each with a different high/low/close -- so a signal
                # can appear and vanish within one bar, and live behaviour
                # stops matching any backtest, which only ever sees closed
                # bars. Only completed bars are evidence.
                data = list(data)
                if data and not USE_DAILY_BARS:
                    last_ts = getattr(data[-1], "timestamp", None)
                    if last_ts is not None:
                        if last_ts.tzinfo is None:
                            last_ts = last_ts.replace(tzinfo=timezone.utc)
                        closes_at = last_ts + timedelta(
                            minutes=BAR_TIMEFRAME_MINUTES)
                        if closes_at > datetime.now(timezone.utc):
                            logger.debug(
                                "OHLC %s — dropping the still-forming bar at %s",
                                symbol, last_ts.isoformat())
                            data = data[:-1]

                if len(data) >= INDICATOR_REQUIRED_BARS:
                    sliced = data[-bars:]
                    highs = [float(b.high) for b in sliced]
                    lows = [float(b.low) for b in sliced]
                    closes = [float(b.close) for b in sliced]
                    opens = [getattr(b, "open", None) for b in sliced]
                    volumes = [getattr(b, "volume", None) for b in sliced]
                    bar_dates = []
                    for b in sliced:
                        timestamp = getattr(b, "timestamp", None)
                        if timestamp is None:
                            bar_dates.append(None)
                        elif USE_DAILY_BARS:
                            bar_dates.append(
                                timestamp.astimezone(timezone.utc).date()
                                if timestamp.tzinfo else timestamp.date())
                        else:
                            # Intraday: keep the full timestamp. Truncating to
                            # a date would make every bar in a session look
                            # like the same bar to staleness checks.
                            bar_dates.append(
                                timestamp.astimezone(timezone.utc)
                                if timestamp.tzinfo else timestamp)
                    # Extract last bar's timestamp for staleness checks
                    try:
                        last_bar_ts = data[-1].timestamp.isoformat() if hasattr(data[-1], 'timestamp') else None
                    except Exception:
                        last_bar_ts = None
                    return {"highs": highs, "lows": lows, "closes": closes, "opens": opens, "volumes": volumes, "bar_dates": bar_dates, "bar_timestamp": last_bar_ts}
                logger.warning(
                    "OHLC fetch for %s — insufficient bars (%d, need >=%d). "
                    "Possible causes: wrong data feed (paper=IEX, live=SIP), "
                    "symbol delisted, or market holiday/weekend.",
                    symbol, len(data), INDICATOR_REQUIRED_BARS,
                )
            except Exception as e:
                logger.error(
                    "OHLC fetch for %s — Alpaca API error: %s",
                    symbol, e,
                    exc_info=True,
                )
                return None

        else:
            logger.warning("OHLC fetch for %s — broker in simulation mode, no real bars", symbol)

        # No real bars available — return None so callers skip signal generation.
        logger.error(
            "OHLC fetch for %s — no real bars returned. "
            "NO SIGNALS will be generated until Alpaca data is available. "
            "Real data only — no synthetic fallback.",
            symbol,
        )
        return None

    def _record_refusal(self, reason: str, symbol: str = "") -> None:
        """Count a declined candidate so zero trades remains explainable."""
        key = reason.strip().lower().replace(" ", "_")
        self.state.refusal_counts[key] = self.state.refusal_counts.get(key, 0) + 1
        self.state.latest_refusals[key] = self.state.latest_refusals.get(key, 0) + 1
        logger.debug("REFUSAL%s: %s", " [%s]" % symbol if symbol else "", key)

    def _paper_exploration_gate(
        self, pattern_hash: str, side: Optional[str], raw_conviction: float,
        market_open: bool, strategy: str, regime: str,
    ) -> tuple:
        """Authorize only statistically promoted, one-share PAPER exploration."""
        blockers = []
        try:
            evidence = self.shadow.evidence(
                pattern_hash,
                minimum_trades=SHADOW_PROMOTION_MIN_TRADES,
                minimum_days=SHADOW_PROMOTION_MIN_DAYS,
                side=side,
                strategy=strategy,
                regime=regime,
            )
        except Exception as exc:
            return False, "shadow evidence unavailable: %s" % exc, {
                "paper_exploration_eligible": False,
                "error": str(exc),
            }
        if not evidence.get("paper_exploration_eligible"):
            return False, "shadow evidence gate not met", evidence
        if side != "buy":
            blockers.append("paper exploration is long-only")
        if abs(raw_conviction) < self.high_conviction:
            blockers.append("raw conviction below actionable threshold")
        if not self.state.is_autonomous:
            blockers.append("not autonomous")
        if not market_open:
            blockers.append("market closed")
        if self.state.news_fetch_degraded:
            blockers.append("news ingestion degraded")
        if self.state.startup_recovery_blocked:
            blockers.append("startup recovery incomplete")
        if self.state.health_failed_this_session:
            blockers.append("pre-market health failed")
        if self.state.paper_exploration_trades_today >= PAPER_EXPLORATION_DAILY_CAP:
            blockers.append("paper exploration daily cap reached")

        broker = self.trading.broker
        if broker.is_simulating or getattr(broker, "environment", None) != "paper":
            blockers.append("paper broker required")
        else:
            try:
                positions = self.trading.get_broker_positions()
                if positions:
                    blockers.append("an account position is already open")
            except Exception as exc:
                blockers.append("broker position truth unavailable: %s" % exc)

        allowed, safety_reason = self.authorize_entry("shadow-promoted exploration")
        if not allowed:
            blockers.append(safety_reason)
        return not blockers, "; ".join(blockers) if blockers else "authorized", evidence

    def _tier2_exploration_gate(self, market_open: bool) -> tuple:
        """Authorize a small PAPER-only Tier-2 RSI-threshold exploration trade.

        Unlike _paper_exploration_gate, this does not require pre-existing
        shadow evidence and is not long-only -- the whole point is to gather
        fresh, symmetric win/loss data on both the oversold and overbought
        grey zones for _generate_tier2_threshold_advisory() to compare
        against the Tier-1 baseline. It can never place a live order.
        """
        blockers = []
        if not self.state.is_autonomous:
            blockers.append("not autonomous")
        if not market_open:
            blockers.append("market closed")
        if self.state.news_fetch_degraded:
            blockers.append("news ingestion degraded")
        if self.state.startup_recovery_blocked:
            blockers.append("startup recovery incomplete")
        if self.state.health_failed_this_session:
            blockers.append("pre-market health failed")
        if self.state.tier_2_trades_today >= TIER_2_DAILY_CAP:
            blockers.append("tier 2 exploration daily cap reached")

        broker = self.trading.broker
        if broker.is_simulating or getattr(broker, "environment", None) != "paper":
            blockers.append("paper broker required")

        allowed, safety_reason = self.authorize_entry("tier-2 RSI exploration")
        if not allowed:
            blockers.append(safety_reason)
        return not blockers, "; ".join(blockers) if blockers else "authorized"

    def _record_filled_pattern(
        self, symbol: str, side: str, conviction: float, rsi_value: float,
        exec_result, signal_dict: dict, strategy: str, regime: str, tier: str,
    ) -> None:
        """Persist the exact indicators used by a filled decision."""
        indicators = (self.state.live_indicators or {}).get(symbol) or {}
        ema_short = indicators.get("ema_short")
        ema_long = indicators.get("ema_long")
        if ema_short is None or ema_long is None:
            raise RuntimeError("live EMA identity unavailable after fill")
        rid, pattern_hash = self.patterns.record_trade_pattern_and_track(
            symbol=symbol,
            sentiment_score=0.0,
            conviction_score=conviction,
            rsi_value=rsi_value,
            ema_short=ema_short,
            ema_long=ema_long,
            prev_ema_short=indicators.get("prev_ema_short"),
            prev_ema_long=indicators.get("prev_ema_long"),
            entry_price=exec_result.filled_price,
            quantity=exec_result.filled_qty or exec_result.quantity,
            side=side,
            tier=tier,
        )
        signal_dict["pattern_record_id"] = rid
        signal_dict["pattern_hash"] = pattern_hash
        self.trading.persist_filled_trade(
            symbol=symbol, pattern_id=rid, strategy=strategy, regime=regime)
        logger.info(
            "Pattern tracked [%s]: record_id=%d hash=%s tier=%s",
            symbol, rid, pattern_hash, tier,
        )


    def _compute_ema(self, prices: List[float], period: int) -> Optional[float]:
        """Compute EMA via patterns.compute_ema, returning None on failure."""
        try:
            result = _import_patterns().compute_ema(prices, period)
            if result is None:
                logger.warning("Cannot compute EMA(%d) — insufficient data (%d bars)", period, len(prices))
            return result
        except Exception as e:
            logger.warning("EMA(%d) computation failed: %s", period, e)
            return None


    def _pipeline_loop(self) -> None:
        """Main pipeline loop: runs periodically with kill-switch protection."""
        logger.info("Pipeline loop started (interval=%ds)", self.poll_interval)

        try:
            # Run first cycle immediately
            self._safe_run_cycle()

            while not self._stop_event.is_set() and self.state.running:
                # Wait for the interval (check every second for early stop)
                for _ in range(self.poll_interval):
                    if self._stop_event.is_set() or not self.state.running:
                        break
                    time.sleep(1)
                else:
                    self._safe_run_cycle()
        except Exception as e:
            self._trigger_kill_switch(e, "_pipeline_loop")

        logger.info("Pipeline loop stopped")


    def _position_monitor_loop(self) -> None:
        """
        Dedicated fast-track loop that polls open positions for stop/target
        hits far more frequently than the full pipeline cycle. This bounds the
        worst-case time a breached position stays open to
        POSITION_MONITOR_INTERVAL_S rather than the (much slower) poll_interval.

        Also checks the file-based kill switch on every tick so the bot
        responds instantly to an external kill signal.
        """
        logger.info(
            "Position monitor started (interval=%ds)", POSITION_MONITOR_INTERVAL_S
        )
        while not self._stop_event.is_set() and self.state.running:
            # Belt-and-suspenders: check file-based kill switch every tick
            try:
                if self._check_file_kill_switch():
                    logger.critical(
                        "Position monitor: KILL_SWITCH file detected — "
                        "halting monitor"
                    )
                    break
            except Exception:
                pass

            # Only manage positions while actively trading (not kill-switched).
            if not self.state.killed and self.state.mode not in (
                OrchestratorMode.STOPPED,
                OrchestratorMode.DAILY_LOSS_LIMIT,
            ):
                try:
                    closed = self._check_active_positions(context="fast-monitor")
                    if closed:
                        self.patterns.db.record_milestone(
                            milestone_type="cycle_check",
                            value=0,
                            note=f"Fast-monitor: closed {len(closed)} positions",
                        )
                except Exception as e:
                    # A monitor failure must never crash the process or trip the
                    # kill switch by itself — just log and continue.
                    logger.error("Position monitor check failed: %s", e)

            for _ in range(POSITION_MONITOR_INTERVAL_S):
                if self._stop_event.is_set() or not self.state.running:
                    break
                time.sleep(1)
        logger.info("Position monitor stopped")

    # ------------------------------------------------------------------
    # Daily Loss Limit Tracking
    # ------------------------------------------------------------------

    def _run_daily_backup(self) -> dict:
        """
        Daily after-hours backup: export data to gzipped snapshots in the
        repo's /data folder, then git-commit-push so learning survives a
        sandbox reset.

        Exports: patterns.db, audit_log.jsonl, trade outcomes, reconciliation
        report, overnight risk snapshot, and backfill status.

        30-day rotation: keeps only the last 30 daily snapshots.
        """
        import gzip
        import json
        import shutil
        from datetime import timedelta
        from pathlib import Path

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if self.state.backup_date == today:
            return {"status": "skipped", "reason": "already_backed_up_today"}

        logger.info("=== Daily Backup: %s ===", today)
        _repo_root = os.path.dirname(APP_ROOT)
        repo_data = Path(_repo_root) / "data"
        repo_data.mkdir(parents=True, exist_ok=True)
        snap_dir = repo_data / today
        snap_dir.mkdir(parents=True, exist_ok=True)
        backed_up = []

        for fname, src, mode in [
            ("patterns.db.gz", "patterns.db", "rb"),
            ("audit_log.jsonl.gz", "audit_log.jsonl", "rb"),
        ]:
            p = Path(DATA_DIR) / src
            if p.exists():
                with open(p, mode) as f_in:
                    with gzip.open(snap_dir / fname, "wb") as f_out:
                        shutil.copyfileobj(f_in, f_out)
                backed_up.append(fname)

        try:
            conn = self.patterns.db._connect()
            trades = conn.execute(
                "SELECT * FROM pattern_memory ORDER BY timestamp DESC LIMIT 5000"
            ).fetchall()
            if trades:
                with gzip.open(snap_dir / "trade_outcomes.json.gz", "wt", encoding="utf-8") as f:
                    json.dump([dict(r) for r in trades], f, default=str)
                backed_up.append("trade_outcomes.json.gz")
        except Exception as te:
            logger.warning("Trade backup failed: %s", te)

        for fname, src in [
            ("reconciliation.json.gz", "reconciliation_latest.json"),
            ("overnight_risk.json.gz", "overnight_risk_latest.json"),
            ("backfill_status.json.gz", "backfill_status.json"),
        ]:
            p = Path(DATA_DIR) / src
            if p.exists():
                with open(p, "rb") as f_in:
                    with gzip.open(snap_dir / fname, "wb") as f_out:
                        shutil.copyfileobj(f_in, f_out)
                backed_up.append(fname)

        logger.info("Backed up %d files to %s", len(backed_up), snap_dir)

        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        removed = 0
        for d in sorted(repo_data.iterdir()):
            if d.is_dir():
                try:
                    if datetime.strptime(d.name, "%Y-%m-%d") < cutoff:
                        shutil.rmtree(d)
                        removed += 1
                except ValueError:
                    pass
        if removed:
            logger.info("Rotated out %d old backup(s)", removed)

        try:
            # Remote push is handled by data_backup.run_daily_backup() (called
            # separately in the after-hours phase).  Local snapshot is complete.
            logger.info("Local snapshot complete — %d files in %s", len(backed_up), snap_dir)
        except Exception as ge:
            logger.error("Backup finalize failed: %s", ge)

        self.state.backup_date = today
        return {"status": "ok", "date": today, "files": backed_up, "rotation_removed": removed}


    def _run_historical_backfill(self) -> dict:
        """
        Startup backfill: fetch the last 200 daily bars for SPY, QQQ, IWM
        from Alpaca and load them into the daily_bars table.

        Uses StockHistoricalDataClient directly from env vars, independent
        of the broker's simulation state.  Logs per-symbol counts and
        surfaces the full Alpaca error on failure.
        """
        symbols = list(TRADING_SYMBOLS)
        backfill_count = 200
        backfill_start = time.time()
        logger.info("=== Startup backfill: fetching %d daily bars for %s ===", backfill_count, symbols)

        results = {}
        total_stored = 0
        any_failed = False

        for symbol in symbols:
            symbol_bars = []
            try:
                from alpaca.data.historical import StockHistoricalDataClient
                from alpaca.data.enums import DataFeed
                from alpaca.data.requests import StockBarsRequest
                from alpaca.data.timeframe import TimeFrame
                from datetime import datetime, timedelta

                key = os.environ.get("APCA_API_KEY_ID", "")
                secret = os.environ.get("APCA_API_SECRET_KEY", "")
                if not key or not secret:
                    logger.error(
                        "Backfill: APCA_API_KEY_ID/APCA_API_SECRET_KEY not set — cannot fetch %s",
                        symbol,
                    )
                    any_failed = True
                    results[symbol] = {"bars_stored": 0, "error": "missing credentials"}
                    continue

                client = StockHistoricalDataClient(key, secret)
                market_now = self.clock.now_ct()
                end_date = market_now.date()
                if (not self.clock.is_trading_day(end_date)
                        or market_now.time() < RTH_CLOSE):
                    end_date -= timedelta(days=1)
                while not self.clock.is_trading_day(end_date):
                    end_date -= timedelta(days=1)
                end_time = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
                req = StockBarsRequest(
                    symbol_or_symbols=[symbol],
                    timeframe=TimeFrame.Day,
                    start=end_time - timedelta(days=730),
                    end=end_time,
                    feed=DataFeed.IEX,  # Paper accounts are entitled to IEX, not SIP
                )
                resp = client.get_stock_bars(req)
                data = resp.data.get(symbol, []) if hasattr(resp, "data") else []
                if not data:
                    logger.critical("Backfill: Alpaca returned 0 bars for %s through closed session %s. Response: %s", symbol, end_date, resp)
                    any_failed = True
                    results[symbol] = {"bars_stored": 0, "error": "empty response"}
                    continue
                received_dates = [
                    b.timestamp.astimezone(timezone.utc).date() if b.timestamp.tzinfo else b.timestamp.date()
                    for b in data if getattr(b, "timestamp", None) is not None
                ]
                max_received = max(received_dates, default=None)
                if max_received != end_date:
                    logger.critical("Backfill: short window for %s; expected latest closed session %s, received %s; refusing partial storage", symbol, end_date, max_received)
                    any_failed = True
                    results[symbol] = {"bars_stored": 0, "error": f"short window (expected {end_date}, got {max_received})"}
                    continue

                for b in data:
                    timestamp = getattr(b, "timestamp", None)
                    if timestamp is None:
                        logger.warning("Backfill: rejecting bar without timestamp for %s", symbol)
                        continue
                    bar_date = timestamp.astimezone(timezone.utc).date() if timestamp.tzinfo else timestamp.date()
                    if not self.clock.is_trading_day(bar_date):
                        logger.warning("Backfill: rejecting non-trading-day bar %s %s", symbol, bar_date)
                        continue
                    date_str = str(bar_date)
                    try:
                        self.patterns.db.store_daily_bar(
                            symbol=symbol,
                            date_str=date_str,
                            open_p=float(b.open),
                            high=float(b.high),
                            low=float(b.low),
                            close=float(b.close),
                            volume=int(b.volume) if hasattr(b, "volume") else 0,
                        )
                        symbol_bars.append(date_str)
                    except Exception as de:
                        logger.debug("Bar store skipped %s %s: %s", symbol, date_str, de)

                logger.info(
                    "Loaded %d bars for %s",
                    len(symbol_bars), symbol,
                )
                results[symbol] = {"bars_stored": len(symbol_bars)}

            except Exception as e:
                logger.error(
                    "Backfill FETCH FAILED for %s: %s",
                    symbol, e,
                )
                any_failed = True
                results[symbol] = {"bars_stored": 0, "error": str(e)}

            total_stored += len(symbol_bars)

        elapsed = time.time() - backfill_start
        samples_str = ", ".join(
            f"{sym}={res['bars_stored']}" for sym, res in results.items()
        )
        logger.info("Backfill complete: %s (%.1fs)", samples_str, elapsed)

        if any_failed:
            self.state.backfill_done = False
            status = "partial"
        else:
            self.state.backfill_done = True
            status = "ok"
        summary = {
            "status": status,
            "symbols": results,
            "total_bars_stored": total_stored,
            "elapsed_seconds": round(elapsed, 2),
            "timestamp": time.time(),
        }
        # Write a status file so the dashboard / API can report backfill state.
        try:
            import json
            status_path = os.path.join(DATA_DIR, "backfill_status.json")
            _write_json_atomic(status_path, summary, indent=2, default=str)
        except Exception as e:
            logger.warning("Failed to write backfill status file: %s", e)
        return summary


    @staticmethod
    def _is_transient_cycle_exception(exception: Exception) -> bool:
        """Return true for known temporary Alpaca/API failures, including wrappers."""
        # Alpaca quote helpers wrap transport errors in RuntimeError; inspect
        # the complete __cause__ chain rather than only the outer error.
        seen = set()
        current = exception
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            status = getattr(current, "status_code", getattr(current, "status", None))
            try:
                if int(status) == 429 or 500 <= int(status) <= 599:
                    return True
            except (TypeError, ValueError):
                pass
            name = type(current).__name__.lower()
            module = type(current).__module__.lower()
            transient_names = ("timeout", "connectionerror", "connecttimeout", "readtimeout", "connectionclosed")
            if any(token in name for token in transient_names):
                return True
            if isinstance(current, (TimeoutError, ConnectionError)):
                return True
            if isinstance(current, OSError) and ("socket" in module or "requests" in module or "httpx" in module):
                return True
            current = current.__cause__
        return False

    def _safe_run_cycle(self) -> None:
        """Run a pipeline cycle with kill-switch protection."""
        # ---- Check file-based kill switch (belt-and-suspenders) ----
        try:
            if self._check_file_kill_switch():
                logger.warning("File-based kill switch active — not running cycle")
                return
        except Exception:
            pass

        if self.state.killed:
            logger.warning("KILLED flag active — not running cycle")
            return

        if self.state.startup_recovery_blocked:
            logger.warning("Startup recovery incomplete — broker truth unavailable; retrying next cycle")
            try:
                reconnect = getattr(self.trading.broker, "reconnect", None)
                if reconnect is not None:
                    reconnect()
                self._recover_positions_on_startup()
            except Exception as retry_exc:
                logger.error("Startup recovery retry failed: %s", retry_exc)
            return
        if self.state.health_failed_this_session:
            logger.warning(
                "Health check failed this session — not running cycle "
                "(retries at next phase transition)"
            )
            return

        if self.state.mode == OrchestratorMode.STOPPED:
            logger.warning("STOPPED mode — not running cycle")
            return

        try:
            self._run_pipeline_cycle()
        except Exception as e:
            if self._is_transient_cycle_exception(e):
                self.state.consecutive_transient_cycle_failures += 1
                count = self.state.consecutive_transient_cycle_failures
                logger.warning(
                    "Transient cycle failure %s on cycle %d (%d/%d): %s",
                    type(e).__name__, self.state.cycle_count, count,
                    TRANSIENT_CYCLE_FAILURE_THRESHOLD, e,
                )
                if count >= TRANSIENT_CYCLE_FAILURE_THRESHOLD:
                    self._trigger_kill_switch(e, "_run_pipeline_cycle (transient threshold)")
            else:
                self._trigger_kill_switch(e, "_run_pipeline_cycle")
        else:
            self.state.consecutive_transient_cycle_failures = 0

        # ---- Write heartbeat after every successful cycle ----
        try:
            self._write_heartbeat()
        except Exception:
            pass


    def _trigger_kill_switch(
        self, exception: Exception, context: str = ""
    ) -> None:
        """
        CRITICAL: Immediately halt all trading and notify the lead.
        Called when an unexpected exception occurs in the trading pipeline.
        """
        import traceback

        tb_str = traceback.format_exc()
        error_msg = f"{exception}\n{tb_str[:500]}"
        # Halt order submission at the ledger too, not just this loop.
        self._engage_execution_kill(True)
        log_msg = f"KILL_SWITCH_TRIGGERED in {context}: {error_msg[:200]}"

        logger.critical("🔴 %s", log_msg)

        # 1. Halt trading — set killed flag + persist to disk
        self.state.killed = True
        self.state.running = False
        self.state.errors.append(log_msg)
        self._write_killed_state_file()
        logger.warning(
            "KILLED flag set (trigger: %s) — persisted to %s",
            context, KILLED_STATE_FILE,
        )

        # 2. Insert critical alert into local alerts database
        try:
            from alert_db import insert_alert
            insert_alert("KILL_SWITCH_TRIGGERED", log_msg, "critical")
        except Exception as db_err:
            logger.error("Failed to log kill switch alert: %s", db_err)

        # 3. Send message to lead
        try:
            from monitoring import send_pending_notifications
            # Write notification file for lead
            import json
            notif_dir = os.path.join(DATA_DIR, "notifications")
            os.makedirs(notif_dir, exist_ok=True)
            notif_path = os.path.join(notif_dir, f"killswitch_{int(time.time())}.json")
            with open(notif_path, "w") as f:
                json.dump({
                    "type": "KILL_SWITCH_TRIGGERED",
                    "severity": "critical",
                    "message": (
                        f"🔴 KILL SWITCH TRIGGERED in {context}\n"
                        f"Exception: {exception}\n"
                        f"Traceback (first 500 chars):\n{tb_str[:500]}"
                    ),
                    "timestamp": time.time(),
                }, f)
            send_pending_notifications()
        except Exception as notif_err:
            logger.error("Failed to send kill switch notification: %s", notif_err)

        logger.critical(
            "🔴 KILLED flag set — System halted. "
            "Call POST /api/reset to clear the killed flag and resume."
        )

    # ------------------------------------------------------------------
    # Position Safety: stop-loss / take-profit enforcement
    # ------------------------------------------------------------------

    def _update_daily_tracking(self) -> None:
        """
        Update daily P&L tracking. Detects day boundaries to reset tracking
        state, and auto-recovers from DAILY_LOSS_LIMIT mode on a new day.
        Called at the start of every pipeline cycle.
        """
        try:
            acct = self.trading.broker.get_account()
            current_equity = float(acct.equity)
        except Exception as e:
            logger.warning("Daily tracking: could not get account equity: %s", e)
            return

        # Guard: reject equity <= $1.00 to prevent false P&L/drawdown
        # triggers from broker glitches (returns $0.00 or missing data).
        # Three consecutive failures escalate to CRITICAL but never kill.
        if current_equity <= 1.0:
            self.state.equity_read_failures += 1
            if self.state.equity_read_failures >= 3:
                logger.critical(
                    "Daily tracking: equity=%.2f invalid (<=$1.00) for %d consecutive cycles — "
                    "broker may be unavailable. No kill, retrying next cycle.",
                    current_equity, self.state.equity_read_failures,
                )
            else:
                logger.warning(
                    "Daily tracking: equity=%.2f is invalid (<=$1.00) — skipping update "
                    "to avoid false daily loss limit trigger (failure %d/3)",
                    current_equity, self.state.equity_read_failures,
                )
            return

        # Successful equity read — reset failure counter
        self.state.equity_read_failures = 0


        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Check if we're on a new trading day (different date from last recorded)
        if self.state.daily_start_date != today_str:
            old_date = self.state.daily_start_date
            was_halted = self.state.mode == OrchestratorMode.DAILY_LOSS_LIMIT

            # Reset daily tracking
            self.state.daily_starting_equity = current_equity
            self.state.daily_start_date = today_str
            self.state.daily_loss_hit = False
            self.state.daily_pnl_pct = 0.0
            self.state.tier_2_trades_today = 0
            self.state.paper_exploration_trades_today = 0
            self._save_daily_tracking()

            logger.info(
                "Daily tracking: new day %s (was %s), starting equity=$%.2f%s",
                today_str, old_date or "None", current_equity,
                " — auto-reset from DAILY_LOSS_LIMIT mode" if was_halted else "",
            )

            # Restore mode from persistence on day rollover.
            # For DAILY_LOSS_LIMIT, the mode file still holds the pre-halt mode
            # (e.g. AUTONOMOUS), because the loss-limit trigger sets
            # state.mode directly and never calls _save_persisted_mode().
            # Only set_mode() -- the operator's own action -- writes the file.
            # For all other modes the file matches the current mode, so this is
            # a no-op restore.
            #
            # This comment previously claimed _save_persisted_mode() was "only
            # called from set_mode()". It was called from the Tier 2 evaluation
            # loop and NOT from set_mode(), so the file was never written by an
            # operator mode change and this restore had nothing to read.
            if was_halted:
                self._load_persisted_mode()
                logger.info(
                    "Day rollover: restored mode from persistence: %s",
                    self.state.mode.value,
                )

            # Record milestone for the new-day reset
            try:
                self.patterns.db.record_milestone(
                    milestone_type="daily_reset",
                    value=current_equity,
                    note=f"New day {today_str}: equity=${current_equity:.2f}"
                    + (", recovered from daily loss limit" if was_halted else ""),
                )
            except Exception:
                pass

            return

        # Same day — update running P&L percentage
        if (
            self.state.daily_starting_equity is not None
            and self.state.daily_starting_equity > 0
        ):
            self.state.daily_pnl_pct = round(
                (current_equity - self.state.daily_starting_equity)
                / self.state.daily_starting_equity
                * 100,
                4,
            )
            self._save_daily_tracking()


    def _write_heartbeat(self) -> None:
        """
        Write a timestamped heartbeat file to disk every pipeline cycle.

        This allows external monitoring to detect if the bot has crashed
        by checking if the heartbeat file is stale.
        """
        try:
            mh = self.state.market_hours
            heartbeat = {
                "timestamp": time.time(),
                "datetime_utc": datetime.now(timezone.utc).isoformat(),
                "cycle_number": self.state.cycle_count,
                "phase": mh.get("phase", "unknown"),
                "mode": self.state.mode.value,
                "status": "alive",
            }
            import json
            os.makedirs(os.path.dirname(self.HEARTBEAT_FILE), exist_ok=True)
            # Write to tmp then rename for atomicity
            tmp = self.HEARTBEAT_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(heartbeat, f, indent=2)
            os.rename(tmp, self.HEARTBEAT_FILE)
        except Exception as e:
            logger.warning("Failed to write heartbeat: %s", e)

    # ------------------------------------------------------------------
    # Pipeline Loop
    # ------------------------------------------------------------------


    def stop(self) -> None:
        """Stop the orchestrator gracefully."""
        self.state.running = False
        self._stop_event.set()
        if self._api_server is not None:
            self._api_server.shutdown()
            self._api_server.server_close()
            self._api_server = None
        logger.info("Orchestrator stopping...")

    def _recover_positions_on_startup(self) -> None:
        """
        Recover tracked positions from the persisted state file and reconcile
        with the broker on startup.

        Called during Orchestrator.start() before the pipeline threads begin.
        If the bot crashed, this ensures:
          - Positions the broker has but we lost track of are adopted
          - Positions we think are open but the broker already closed are cleaned
          - Orders left mid-submit ("reserved") are resolved against the broker
          - All mismatches are logged at CRITICAL severity
        """
        # Resolve the ORDER ledger first: an interrupted submit leaves a
        # reserved record with no broker id, which blocks its symbol forever
        # until someone asks the broker whether the order actually landed.
        try:
            truth = self.trading.position_truth
            if truth is not None:
                ledger_report = truth.reconcile()
                logger.info(
                    "Order ledger reconciled: %d orders resolved, status=%s",
                    ledger_report.get("orders_reconciled", 0),
                    ledger_report.get("status"),
                )
                for symbol, snapshot in (ledger_report.get("conflicts") or {}).items():
                    logger.critical(
                        "STARTUP CONFLICT on %s: %s — trading blocked for this "
                        "symbol until resolved.",
                        symbol, "; ".join(snapshot.get("reasons", [])),
                    )
        except Exception as exc:
            logger.error(
                "Order ledger reconciliation failed at startup: %s. "
                "Entry gating will still fail closed.", exc,
            )
        logger.info("=== Startup Recovery: Position State Reconciliation ===")
        try:
            from position_state import PositionStateManager
            mgr = PositionStateManager()
            state_positions = mgr.load_positions()

            # Simulation has no broker position endpoint by design. An empty
            # local state is the valid flat starting point for a dry run; do
            # not turn that into a false startup-recovery block. If simulated
            # positions already exist, fail closed because there is no broker
            # truth available to reconcile them against.
            if self.trading.broker.is_simulating:
                if state_positions:
                    raise RuntimeError(
                        "simulation mode cannot reconcile persisted positions "
                        "without broker truth"
                    )
                logger.info(
                    "Simulation mode: no broker reconciliation required; "
                    "starting flat"
                )
                self.state.startup_recovery_blocked = False
                return

            if not state_positions:
                logger.info("No persisted position state found — validating broker positions.")

            # Fetch current broker positions
            broker_positions = self.trading.get_broker_positions()

            # Run reconciliation
            result = mgr.reconcile(broker_positions)

            # Log results
            adopted = result.get("adopted", [])
            cleaned = result.get("cleaned", [])
            inconsistencies = result.get("inconsistencies", [])

            if adopted:
                logger.warning(
                    "RECOVERY: Adopted %d positions from broker that were "
                    "not in our state file: %s",
                    len(adopted), [a["symbol"] for a in adopted],
                )
                # Re-register adopted positions in pattern DB so they're
                # monitored by the fast-track position monitor.
                for a in adopted:
                    try:
                        self.patterns.db.add_active_position(
                            # Unique per symbol. record_id is the PRIMARY
                            # KEY and the insert is INSERT OR REPLACE, so the
                            # old `record_id=0` placeholder meant every
                            # adopted position overwrote the previous one --
                            # three recovered, one tracked.
                            record_id=self.patterns.db.adopted_record_id(
                                a["symbol"]),
                            symbol=a["symbol"],
                            entry_price=float(a.get("entry_price", 0)),
                            quantity=int(abs(float(a.get("qty", 0)))),
                            side=a.get("side", "buy"),
                            pattern_hash="recovered",
                            conviction=0.0,
                        )
                        logger.info(
                            "  Adopted %s: qty=%s @ $%.2f",
                            a["symbol"], a.get("qty"), float(a.get("entry_price", 0)),
                        )
                    except Exception as e:
                        logger.error(
                            "Failed to adopt position %s: %s", a["symbol"], e,
                        )

            if cleaned:
                logger.critical(
                    "RECOVERY: Cleaned %d stale positions from state file "
                    "that no longer exist at broker: %s",
                    len(cleaned), [c["symbol"] for c in cleaned],
                )

            if inconsistencies:
                logger.critical(
                    "RECOVERY: %d position inconsistencies found and corrected: %s",
                    len(inconsistencies),
                    [i["symbol"] for i in inconsistencies],
                )

            # Record milestone
            try:
                self.patterns.db.record_milestone(
                    milestone_type="startup_recovery",
                    value=len(adopted),
                    note=(
                        f"Startup recovery: {len(adopted)} adopted, "
                        f"{len(cleaned)} cleaned, {len(inconsistencies)} inconsistencies"
                    ),
                )
            except Exception:
                pass

            if result.get("status") == "inconsistent":
                logger.warning(
                    "Startup recovery completed with %d inconsistencies — "
                    "positions reconciled.",
                    len(inconsistencies) + len(cleaned),
                )
            else:
                logger.info("Startup recovery completed — all positions consistent.")

            self.state.startup_recovery_blocked = False
        except Exception as e:
            self.state.startup_recovery_blocked = True
            logger.error("Startup position recovery failed; trading blocked until broker retry succeeds: %s", e)
            try:
                from alert_db import insert_alert
                insert_alert("STARTUP_RECOVERY_FAILED", "Broker positions could not be fetched; trading blocked until retry", "critical")
            except Exception:
                pass
            import traceback
            logger.debug("Recovery traceback: %s", traceback.format_exc())

    def set_mode(self, mode: str) -> dict:
        """
        Set the orchestrator mode — operator-initiated only.

        Automated conditions (kills, health checks) use internal flags
        (killed, health_failed_this_session) instead — they must NEVER
        call set_mode().

        Args:
            mode: "manual", "autonomous", or "stopped"

        Returns:
            State dict.
        """
        if mode.lower() == "killed":
            raise ValueError(
                "'killed' is an internal flag, not a mode. "
                "Use 'manual', 'autonomous', or 'stopped'."
            )
        mode_map = {
            "manual": OrchestratorMode.MANUAL,
            "autonomous": OrchestratorMode.AUTONOMOUS,
            "stopped": OrchestratorMode.STOPPED,
            "daily_loss_limit": OrchestratorMode.DAILY_LOSS_LIMIT,
        }
        new_mode = mode_map.get(mode.lower())
        if new_mode is None:
            raise ValueError(
                f"Invalid mode '{mode}'. "
                "Options: manual, autonomous, stopped, daily_loss_limit"
            )

        old_mode = self.state.mode
        self.state.mode = new_mode

        # Persist BEFORE acting on the change. The operator's intent is the
        # thing that must survive; if the process dies during start()/stop()
        # below, the file should already reflect what was asked for.
        #
        # This call was missing entirely. set_mode() changed the mode in
        # memory only, so every restart silently reverted to MANUAL --
        # including systemd's Restart=always after a crash. The bot would
        # come back up, log PREFLIGHT PASSED, cycle normally, and never trade
        # again, with nothing announcing that it had stopped.
        #
        # Observed: autonomous set at 09:45, restarted at 10:30, then a full
        # afternoon of cycles in MANUAL and an empty decision journal. It
        # looked like a broken signal path and was a lost setting.
        #
        # docs/MODE_PRECEDENCE.md already documented this as working.
        self._save_persisted_mode()

        if new_mode == OrchestratorMode.STOPPED:
            self.stop()
        elif old_mode == OrchestratorMode.STOPPED and new_mode != OrchestratorMode.STOPPED:
            self.start()

        logger.warning(
            "Operator mode changed: %s → %s (operator-initiated API call)",
            old_mode.value, new_mode.value,
        )
        return self.get_state()

    def reset(self) -> dict:
        """Reset orchestrator — clear killed/health flags, restore operator mode."""
        self.state.killed = False
        self.state.health_failed_this_session = False
        # Remove kill-state file
        try:
            if os.path.exists(KILLED_STATE_FILE):
                os.remove(KILLED_STATE_FILE)
        except Exception:
            pass
        self._load_persisted_mode()
        logger.warning(
            "Orchestrator reset — killed/health flags cleared, "
            "mode restored from operator file: %s",
            self.state.mode.value,
        )
        return self.get_state()

    # ------------------------------------------------------------------
    # Remote Kill Switch
    # ------------------------------------------------------------------
    KILL_SWITCH_FILE = os.path.join(DATA_DIR, "KILL_SWITCH")
    HEARTBEAT_FILE = os.path.join(DATA_DIR, "heartbeat")

    def kill(self) -> dict:
        """
        Remote kill switch: immediately close all positions, switch to KILLED
        mode, and prevent any further trading until explicitly reset.
        """
        logger.critical("🔴 REMOTE KILL SWITCH ACTIVATED via API")

        # 1. Close all open positions
        try:
            closed = self._close_all_active_positions()
            logger.critical("Kill: closed %d positions", len(closed))
        except Exception as e:
            logger.error("Kill: close positions failed: %s", e)

        # 2. Clear local position state ONLY if the broker confirms we are
        #    genuinely flat. Wiping it while positions are still live would
        #    make them invisible to us while they keep running -- the exact
        #    opposite of what a kill switch is for.
        try:
            from position_state import PositionStateManager
            still_open = []
            for position in PositionStateManager().load_positions():
                symbol = position.get("symbol")
                try:
                    if self.trading.broker.get_position_strict(symbol):
                        still_open.append(symbol)
                except Exception as probe_error:
                    still_open.append(symbol)
                    logger.error(
                        "Kill: could not confirm %s is flat (%s); keeping state.",
                        symbol, probe_error,
                    )
            if still_open:
                logger.critical(
                    "Kill: NOT clearing position state — still open or "
                    "unconfirmed at broker: %s. These positions remain tracked "
                    "and require manual attention.", ", ".join(still_open),
                )
            else:
                PositionStateManager().clear_all(confirmed_flat=True)
                logger.critical("Kill: broker confirmed flat; state cleared.")
        except Exception as e:
            logger.error("Kill: position state check failed: %s", e)

        # 3. Set killed flag + persist to disk
        self._engage_execution_kill(True)
        self.state.killed = True
        self.state.errors.append("REMOTE KILL SWITCH ACTIVATED")
        self._write_killed_state_file()
        logger.warning(
            "KILLED flag set (trigger: API kill) — persisted to %s",
            KILLED_STATE_FILE,
        )

        # 4. Write the KILL_SWITCH file so the file-based check also catches it
        try:
            with open(self.KILL_SWITCH_FILE, "w") as f:
                f.write(f"KILLED at {time.time()}\n")
        except Exception as e:
            logger.error("Kill: failed to write KILL_SWITCH file: %s", e)

        # 5. Record milestone
        try:
            self.patterns.db.record_milestone(
                milestone_type="kill_switch",
                value=0,
                note="Remote kill switch activated",
            )
        except Exception:
            pass

        logger.critical(
            "🔴 KILLED flag set — System halted. "
            "Remove KILL_SWITCH / RESET_DRAWDOWN files and call reset to resume."
        )
        return self.get_state()

    def _run_pipeline_cycle(self) -> dict:
        """
        Execute one full pipeline cycle.
          1. Fetch bars and compute indicators (RSI, EMA 20/50, ADX)
          2. Classify the regime; derive conviction from trend strength
             normalised by volatility
          3. Cross-reference with pattern memory
          4. Evaluate the trading signal
          5. Execute if autonomous and the gates allow it

        News is still fetched for context and telemetry, but sentiment was
        removed from the signal path: headline sentiment on SPY/QQQ/IWM is not
        a defensible edge, and leaving it in the docstring made this cycle look
        like something it no longer is.
        """
        cycle_start = time.time()
        self.state.cycle_count += 1
        cycle_num = self.state.cycle_count
        self.state.latest_refusals = {}

        # Reset reference-price tracking flags at the start of each cycle.
        # Execute() and _calculate_quantity() will set these during the cycle.
        self.trading.ref_price_failed = False
        self.trading.ref_price_succeeded = False

        logger.info("=== Pipeline Cycle #%d ===", cycle_num)
        try:
            counts = {}
            for sym in TRADING_SYMBOLS:
                bars = self.patterns.db.get_recent_daily_bars(sym, limit=9999)
                counts[sym] = len(bars)
            logger.info("Cycle %d: daily_bars row counts -- %s", cycle_num, str(counts))
        except Exception:
            pass

        result = {
            "cycle": cycle_num,
            "timestamp": cycle_start,
            "steps": {},
        }

        # ---- Step 0a: Daily Loss Limit Check ----
        # Must run before any other logic so the system never trades into an
        # escalating drawdown. This also handles auto-recovery on day change.
        try:
            limit_hit = self._check_daily_loss_limit()
            if self.state.killed:
                logger.warning(
                    "Cycle #%d — KILLED flag active, no trading permitted",
                    cycle_num,
                )
                limit_hit = True
            if not limit_hit and self.state.drawdown_killed:
                logger.critical(
                    "Drawdown kill active -- no trading permitted until explicitly reset"
                )
                limit_hit = True
            result["daily_loss_limit"] = {
                "breached": limit_hit,
                "daily_pnl_pct": self.state.daily_pnl_pct,
                "daily_loss_hit": self.state.daily_loss_hit,
            }
            if limit_hit or self.state.mode == OrchestratorMode.DAILY_LOSS_LIMIT:
                if limit_hit:
                    logger.critical(
                        "⚠️ Cycle #%d skipped — daily loss limit breached (%.2f%%)",
                        cycle_num, self.state.daily_pnl_pct,
                    )
                else:
                    logger.info(
                        "Cycle #%d — DAILY_LOSS_LIMIT mode, tracking only (no trading)",
                        cycle_num,
                    )
                self._finalize_cycle(cycle_start, result)
                return result
        except Exception as e:
            logger.error("Daily loss limit check FAILED: %s", e)
            result["daily_loss_limit"] = {"status": "error", "error": str(e)}

        # ---- Step 0: Check Active Positions (stop-loss / take-profit) ----
        try:
            closed_results = self._check_active_positions(context=f"cycle #{cycle_num}")
            if closed_results:
                result["closed_positions"] = closed_results
                self.patterns.db.record_milestone(
                    milestone_type="cycle_check",
                    value=0,
                    note=f"Cycle #{cycle_num}: closed {len(closed_results)} positions",
                )
        except Exception as e:
            logger.error("Step 0 FAILED: %s", e)
            result["steps"]["position_check"] = {"status": "error", "error": str(e)}

        # ---- Market-hours check: gate trade execution to RTH ----
        # Pre-market prep (news/sentiment/regime/patterns) still runs so the
        # system is ready at the open; only order EXECUTION is gated.
        was_market_open = bool(self.state.market_hours.get("is_open", False))
        try:
            mh = self.clock.status()
        except Exception as e:
            logger.error("Market-hours check failed: %s", e)
            mh = {"is_open": False, "phase": "unknown", "error": str(e)}
        self.state.market_hours = mh
        market_open = bool(mh.get("is_open"))
        
        # Clear the pre-market health latch when the market opens so
        # the bot doesn't skip the entire trading day.
        if not was_market_open and market_open and self.state.health_failed_this_session:
            logger.info(
                "Market opened (phase=%s) — clearing health_failed_this_session latch",
                mh.get("phase"),
            )
            self.state.health_failed_this_session = False
        phase = mh.get("phase", "unknown")
        
        # Track triggers to run reconciliation and health check once per day/window
        if not hasattr(self, "_last_recon_date"):
            self._last_recon_date = None
        if not hasattr(self, "_last_health_date"):
            self._last_health_date = None
            
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if phase == "pre_market":
            logger.info("PHASE: pre_market. Preparing news, sentiment, patterns & running Pre-Market Health check.")
            if self._last_health_date != today_str:
                self.run_pre_market_health(market_hours=mh)
                self._last_health_date = today_str
                
            # No news/sentiment/trading cycles outside open/pre_market
            return result
            
        elif phase == "after_hours":
            logger.info("PHASE: after_hours. Running After-Hours Reconciliation Engine.")
            if self._last_recon_date != today_str:
                self.run_reconciliation()
                self._last_recon_date = today_str
            # ---- Daily backup after reconciliation (via data_backup module) ----
            try:
                from data_backup import run_daily_backup
                backup_ref = [""]
                if self.state.backup_date:
                    backup_ref[0] = self.state.backup_date
                result = run_daily_backup(
                    patterns_db_path=os.path.join(DATA_DIR, "patterns.db"),
                    backup_date_ref=backup_ref,
                    branch="main",
                )
                self.state.backup_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if result.get("status") == "ok":
                    logger.info("Daily backup complete: %d files", len(result.get("files", [])))
                elif result.get("status") == "push_failed":
                    logger.warning("Daily backup: push failed, data preserved locally")
            except Exception as be:
                logger.error("Daily backup FAILED: %s", be)
            # No news/sentiment/trading cycles outside open/pre_market
            self._finalize_cycle(cycle_start, result)
            return result
            
        elif phase in ("holiday", "weekend"):
            logger.info(f"PHASE: {phase}. Standby — running reconciliation.")
            if self._last_recon_date != today_str:
                self.run_reconciliation()
                self._last_recon_date = today_str
            # ---- Historical data backfill on weekends/holidays ----
            # Fetch real daily bars from Alpaca so RSI/ADX have history to
            # work with when the market opens on Monday. Startup already
            # performs this check, so do not refetch on every 120-second
            # weekend cycle. A failed or partial backfill remains eligible
            # for retry on the next cycle.
            if not self.state.backfill_done:
                try:
                    self._run_historical_backfill()
                except Exception as bfe:
                    logger.error("Weekend backfill FAILED: %s", bfe)
            else:
                logger.info("Weekend backfill skipped — already complete")
            self._finalize_cycle(cycle_start, result)
            return result

        result["steps"]["market_hours"] = {
            "status": "ok",
            "is_open": market_open,
            "phase": mh.get("phase"),
            "next_open": mh.get("next_open"),
            "next_close": mh.get("next_close"),
        }
        # ---- Step 0.5: Compute fresh indicators for this cycle ----
        self._compute_indicators_this_cycle(market_open)

        if not market_open:
            logger.info(
                "Market CLOSED (phase=%s) — standby: prepping data, no trades. "
                "Next open: %s", mh.get("phase"), mh.get("next_open"),
            )

        # ---- Step 1: Fetch News ----
        try:
            headlines = self.news.fetch_headlines(MAX_HEADLINES)
            result["steps"]["news"] = {
                "status": "ok",
                "headline_count": len(headlines),
            }
            logger.info("Step 1: Fetched %d headlines", len(headlines))

            # Category failures are advisory: preserve usable partial data and
            # continue the cycle. The degraded flag is exposed in the result and
            # synchronized each cycle so it cannot become sticky.
            self.state.news_fetch_degraded = getattr(
                self.news, 'news_fetch_degraded', False,
            )
            self.state.news_categories_attempted = getattr(self.news, "categories_attempted", 0)
            self.state.news_categories_failed = getattr(self.news, "categories_failed", 0)
            self.state.news_headlines_retrieved = len(headlines)
            self.state.news_articles_retrieved_total = getattr(self.news, "news_articles_retrieved_total", len(headlines))
            self.state.news_headlines_used = getattr(self.news, "news_headlines_used", len(headlines))
            if self.state.news_fetch_degraded:
                logger.info("News degraded: new entries suppressed; position monitoring and risk management continue.")
                logger.info(
                    "News degraded detail: categories_failed=%d/%d articles_total=%d headlines_used=%d",
                    self.state.news_categories_failed,
                    self.state.news_categories_attempted,
                    self.state.news_articles_retrieved_total,
                    self.state.news_headlines_used,
                )
                result["steps"]["news"] = {
                    "status": "degraded",
                    "headline_count": len(headlines),
                    "error": "One or more Finnhub categories failed; partial results retained",
                }
        except Exception as e:
            logger.error("Step 1 FAILED: %s", e)
            result["steps"]["news"] = {"status": "error", "error": str(e)}
            self.state.errors.append(f"Cycle {cycle_num} news: {e}")
            self._finalize_cycle(cycle_start, result)
            return result

        if not headlines:
            logger.info("No headlines fetched. Skipping cycle.")
            self._finalize_cycle(cycle_start, result)
            return result

        # ---- Step 2: Analyze Sentiment ----
        try:
            sent_result = self.sentiment.analyze(headlines)
            sent_dict = _import_sentiment().quick_batch(headlines)
            self.state.last_sentiment_result = sent_dict

            result["steps"]["sentiment"] = {
                "status": "ok",
                "conviction": sent_result.aggregate_conviction,
                "consensus": sent_result.consensus,
                "volatility": sent_result.volatility_signal,
            }
            # Capture per-headline detail for the audit trail.
            result["audit_headlines"] = [
                {
                    "headline": h.headline,
                    "conviction": h.conviction_score,
                    "label": h.sentiment_label,
                    "confidence": h.confidence,
                    "matched_keywords": list(h.matched_keywords.keys()),
                }
                for h in sent_result.headlines
            ]
            logger.info(
                "Step 2: Sentiment conviction=%.4f (consensus=%s)",
                sent_result.aggregate_conviction, sent_result.consensus,
            )
        except Exception as e:
            logger.error("Step 2 FAILED: %s", e)
            result["steps"]["sentiment"] = {"status": "error", "error": str(e)}
            self.state.errors.append(f"Cycle {cycle_num} sentiment: {e}")
            self._finalize_cycle(cycle_start, result)
            return result

        # ---- Step 2.5: Market Regime Detection (from live indicators) ----
        try:
            if self.state.indicators_valid and self.state.live_indicators:
                regime_info = regime_info_from_indicators(
                    self.state.live_indicators)
                self.state.market_regime = regime_info
            else:
                regime_info = {
                    "regime": "unknown", "adx": None, "strategy": "none",
                    "position_size_factor": 0.0, "detail": {},
                    "updated_at": time.time(),
                }
                self.state.market_regime = regime_info
            result["steps"]["regime"] = {"status": "ok", **regime_info}
        except Exception as e:
            logger.error("Step 2.5 (regime) FAILED: %s", e)
            result["steps"]["regime"] = {"status": "error", "error": str(e)}
            regime_info = self.state.market_regime

        # ---- Step 3: Pattern Cross-Reference ----
        try:
            # Evaluate patterns for major symbols
            primary_symbols = list(TRADING_SYMBOLS)
            pattern_results = []

            for sym in primary_symbols:
                # Use live indicators computed earlier in this cycle
                live = self.state.live_indicators.get(sym, {})
                rsi_value = live.get("rsi")
                if rsi_value is None:
                    logger.warning("  No live RSI for %s — skipping signal (fail closed)", sym)
                    self._record_refusal("indicator unavailable", sym)
                    pattern_results.append({
                        "symbol": sym,
                        "action": "skip",
                        "conviction": 0.0,
                        "rsi_value": None,
                        "historical_samples": 0,
                        "reason": "No live indicator data available",
                    })
                    continue
                # Reuse the exact closed-bar series computed in Step 1. A
                # second fetch can cross a bar boundary and make the decision
                # disagree with the indicator log.
                _ema_s = live.get("ema_short")
                _ema_l = live.get("ema_long")
                if _ema_s is None or _ema_l is None:
                    logger.warning("  ⏭️ Skipping signal for %s — EMA unavailable", sym)
                    self._record_refusal("ema unavailable", sym)
                    pattern_results.append({
                        "symbol": sym,
                        "action": "skip",
                        "conviction": 0.0,
                        "rsi_value": rsi_value,
                        "historical_samples": 0,
                        "reason": "EMA unavailable this cycle",
                    })
                    continue
                # Signature inputs are technical only. Feeding sentiment in
                # here would keep splitting patterns along an axis we no
                # longer trade on -- diluting every bucket's sample size for
                # a signal we removed. The space collapses from ~45 to ~15
                # patterns, which also lowers the multiple-testing bar.
                _live = self.state.live_indicators.get(sym, {})
                _tconv = _import_patterns().trend_conviction(
                    _live.get("adx"), _ema_s, _ema_l, _live.get("volatility_pct"))
                _symbol_strategy = (
                    (regime_info.get("detail") or {}).get(sym, {})
                    .get("strategy", "none")
                )
                _signature_conviction = (
                    _import_patterns().mean_reversion_conviction(rsi_value)
                    if _symbol_strategy == "mean_reversion" else _tconv
                )
                pattern_signal = self.patterns.evaluate(
                    symbol=sym,
                    sentiment_score=0.0,
                    conviction_score=_signature_conviction,
                    rsi_value=rsi_value,
                    ema_short=_ema_s,
                    ema_long=_ema_l,
                    prev_ema_short=_live.get("prev_ema_short"),
                    prev_ema_long=_live.get("prev_ema_long"),
                )
                pattern_results.append({
                    "symbol": sym,
                    "action": pattern_signal.action,
                    "conviction": pattern_signal.conviction,
                    "rsi_value": rsi_value,
                    "historical_samples": pattern_signal.pattern_stats.count,
                    "resolved_samples": pattern_signal.pattern_stats.resolved,
                    "pattern_hash": pattern_signal.pattern_signature.hash_id,
                    "pattern_label": pattern_signal.pattern_signature.label,
                    "raw_price_conviction": _signature_conviction,
                    "reason": pattern_signal.reason[:100],
                })
                if pattern_signal.pattern_stats.count == 0:
                    self._record_refusal("no pattern history", sym)
                (logger.debug if pattern_signal.pattern_stats.count == 0 else logger.info)(
                    "  Pattern [%s]: %s (%.3f, samples=%d)",
                    sym, pattern_signal.action.upper(),
                    pattern_signal.conviction,
                    pattern_signal.pattern_stats.count,
                )

            self.state.last_pattern_result = pattern_results

            result["steps"]["patterns"] = {
                "status": "ok",
                "evaluations": pattern_results,
            }
            logger.info("Step 3: Pattern evaluation complete")
        except Exception as e:
            logger.error("Step 3 FAILED: %s", e)
            pattern_results = []
            result["steps"]["patterns"] = {"status": "error", "error": str(e)}

        # ---- Step 4: Trading Decision ----
        try:
            # Create blended signal via TradeSignal.from_engines()
            trade_mod = _import_trading()

            for pr in pattern_results:
                symbol = pr["symbol"]
                pattern_conv = pr["conviction"]
                rsi_value = pr.get("rsi_value", 50.0)
                _ind = self.state.live_indicators.get(symbol, {})
                _pmod = _import_patterns()
                symbol_regime = (regime_info.get("detail") or {}).get(symbol, {
                    "regime": "unknown", "strategy": "none",
                    "position_size_factor": 0.0, "adx": None,
                })
                regime = symbol_regime.get("regime", "unknown")
                strategy = symbol_regime.get("strategy", "none")
                size_factor = symbol_regime.get("position_size_factor", 0.0)

                if strategy == "none":
                    logger.info(
                        "Signal [%s]: skipping entry — strategy=none, regime=%s (indicators unavailable)",
                        symbol, regime,
                    )
                    self._record_refusal("no strategy for symbol regime", symbol)
                    continue

                # Raw price logic is observed separately from the learned
                # pattern gate. It cannot place a normal-size order; it only
                # creates a next-bar shadow outcome.
                raw_conviction = float(pr.get("raw_price_conviction") or 0.0)
                if strategy == "mean_reversion":
                    raw_conviction = _pmod.mean_reversion_conviction(rsi_value)
                raw_side = None
                if raw_conviction >= SHADOW_SIGNAL_THRESHOLD:
                    raw_side = "buy"
                elif raw_conviction <= -SHADOW_SIGNAL_THRESHOLD:
                    raw_side = "sell"

                shadow_recorded = False
                if raw_side and pr.get("pattern_hash"):
                    try:
                        signal_bar_ts = _epoch_timestamp(_ind.get("bar_timestamp"))
                        if signal_bar_ts is None:
                            raise ValueError("closed-bar timestamp unavailable")
                        candidate = _import_shadow_forward().ShadowCandidate(
                            symbol=symbol,
                            signal_bar_ts=signal_bar_ts,
                            side=raw_side,
                            strategy=strategy,
                            conviction=raw_conviction,
                            regime=regime,
                            pattern_hash=pr["pattern_hash"],
                            rsi=float(rsi_value),
                            adx=float(_ind.get("adx")),
                            ema_short=float(_ind.get("ema_short")),
                            ema_long=float(_ind.get("ema_long")),
                            operationally_eligible=bool(
                                market_open and not self.state.news_fetch_degraded),
                        )
                        shadow_recorded = self.shadow.record_candidate(candidate)
                    except Exception as shadow_exc:
                        logger.warning("SHADOW [%s]: candidate refused: %s", symbol, shadow_exc)
                        self._record_refusal("shadow candidate invalid", symbol)

                # Every normal-size production path requires learned pattern
                # evidence. RSI extremes are hypotheses, not an exemption.
                pattern_allowed = learned_pattern_allows_execution(
                    pattern_conv, pr.get("resolved_samples", 0),
                    self.min_conviction)
                if not pattern_allowed:
                    reason = (
                        "pattern outcomes insufficient"
                        if pr.get("resolved_samples", 0) < PATTERN_EXECUTION_MIN_RESOLVED
                        else "pattern conviction too low"
                    )
                    self._record_refusal(reason, symbol)

                side = None
                action = "neutral"
                mean_reversion_applied = False
                blended = 0.0

                if strategy == "mean_reversion" and pattern_allowed:
                    # Range-bound market → FADE RSI extremes instead of
                    # following the sentiment trend. Buy oversold dips, sell
                    # overbought spikes; ignore mid-range (no edge).
                    if rsi_value <= RSI_MEAN_REVERT_OVERSOLD:
                        side, action = "buy", "buy"
                        mean_reversion_applied = True
                    elif rsi_value >= RSI_MEAN_REVERT_OVERBOUGHT:
                        side, action = "sell", "sell"
                        mean_reversion_applied = True
                    # Conviction for a mean-reversion trade scales with how
                    # far RSI is from 50 (the extremity of the fade).
                    if mean_reversion_applied:
                        blended = _pmod.mean_reversion_conviction(rsi_value)
                elif pattern_allowed:
                    # Trending / transitioning / unknown → trend-following.
                    # Direction and strength come from price; the learned
                    # pattern conviction scales it rather than replacing it.
                    _tc = _pmod.trend_conviction(
                        _ind.get("adx"), _ind.get("ema_short"),
                        _ind.get("ema_long"), _ind.get("volatility_pct"))
                    if _tc == 0.0:
                        logger.debug(
                            "Signal [%s]: no trend conviction (adx=%s, ema=%s/%s)",
                            symbol, _ind.get("adx"), _ind.get("ema_short"),
                            _ind.get("ema_long"),
                        )
                    blended = round(_tc * max(0.25, min(1.0, abs(pattern_conv) or 0.25)), 4)
                    if blended >= self.min_conviction:
                        side = "buy"
                        action = "buy" if blended < self.high_conviction else "strong_buy"
                    elif blended <= -self.min_conviction:
                        side = "sell"
                        action = "sell" if blended > -self.high_conviction else "strong_sell"

                # Tier 2 exploration candidate: RSI in the grey zone the
                # Tier-1 gate just missed. Independent of pattern_allowed --
                # this measures raw threshold sensitivity, not a learned
                # pattern -- and only ever applies when Tier-1 did not
                # already claim this symbol this cycle.
                tier2_side = None
                if strategy == "mean_reversion" and not mean_reversion_applied:
                    tier2_side = tier2_grey_zone_side(rsi_value)

                is_high_conviction = abs(blended) >= self.high_conviction

                signal_dict = {
                    "symbol": symbol,
                    "action": action,
                    "side": side,
                    "conviction": round(blended, 4),
                    "sentiment_conviction": None,  # sentiment removed from the signal path
                    "pattern_conviction": round(pattern_conv, 4),
                    "rsi_value": rsi_value,
                    "regime": regime,
                    "strategy": strategy,
                    "position_size_factor": size_factor,
                    "high_conviction": is_high_conviction,
                    "raw_price_conviction": round(raw_conviction, 4),
                    "raw_side": raw_side,
                    "pattern_hash": pr.get("pattern_hash"),
                    "pattern_label": pr.get("pattern_label"),
                    "historical_samples": pr.get("historical_samples", 0),
                    "resolved_samples": pr.get("resolved_samples", 0),
                    "shadow_recorded": shadow_recorded,
                }

                # ---- Step 5: Execute (if autonomous AND market open) ----
                if (
                    self.state.is_autonomous
                    and is_high_conviction
                    and side is not None
                    and market_open
                    and not self.state.news_fetch_degraded
                    and not self.state.startup_recovery_blocked
                    and not self.state.health_failed_this_session
                ):
                    # Apply the regime position-size factor (e.g. ×0.5 in a
                    # transitioning market) around this single execution.
                    _orig_risk = self.trading.risk_per_trade
                    self.trading.risk_per_trade = TIER_1_RISK_PER_TRADE * size_factor * self.state.position_size_multiplier
                    try:
                        signal = trade_mod.TradeSignal(
                            symbol=symbol, action=action, conviction=blended,
                            source="price+pattern+regime",
                            stop_loss_pct=STOP_LOSS_PCT,
                            take_profit_pct=TAKE_PROFIT_PCT,
                            reason=(
                                f"Cycle #{cycle_num} {strategy} "
                                f"(regime={regime}, rsi={rsi_value:.1f})"
                            ),
                        )
                        exec_result = self.trading.execute(
                            signal, overnight_risk=self.carries_overnight_risk())
                    finally:
                        self.trading.risk_per_trade = _orig_risk
                    signal_dict["tier"] = "signal"
                    signal_dict["executed"] = exec_result.success
                    signal_dict["order_id"] = exec_result.order_id
                    signal_dict["filled_price"] = exec_result.filled_price
                    signal_dict["status"] = exec_result.status.value
                    signal_dict["latency_ms"] = exec_result.latency_ms

                    self.state.last_trade_result = signal_dict
                    logger.info(
                        "  🔥 AUTONOMOUS [%s]: %s %d @ %.2f "
                        "(conv=%.3f, latency=%.0fms)",
                        symbol, side.upper(),
                        exec_result.quantity,
                        exec_result.filled_price or 0,
                        blended,
                        exec_result.latency_ms,
                    )

                    # Record pattern entry and track position
                    if exec_result.success and exec_result.filled_price:
                        try:
                            self._record_filled_pattern(
                                symbol, side, raw_conviction, rsi_value, exec_result,
                                signal_dict, strategy, regime, "signal")
                        except Exception as e:
                            logger.warning(
                                "Failed to record pattern for %s: %s", symbol, e
                            )
                else:
                    signal_dict["executed"] = False
                    if not self.state.is_autonomous:
                        signal_dict["reason_blocked"] = "Manual mode — evaluation only"
                    elif not market_open and side is not None and is_high_conviction:
                        signal_dict["reason_blocked"] = (
                            f"Market closed ({self.state.market_hours.get('phase')}) "
                            f"— standby until {self.state.market_hours.get('next_open')}"
                        )
                    elif not is_high_conviction:
                        signal_dict["reason_blocked"] = (
                            f"Conviction {abs(blended):.3f} < threshold "
                            f"{self.high_conviction}"
                        )
                        self._record_refusal("actionable conviction too low", symbol)

                # A cold-start pattern may qualify for exactly one PAPER share
                # only after its separate shadow evidence clears every gate.
                if not signal_dict.get("executed") and raw_side:
                    try:
                        allowed, why, evidence = self._paper_exploration_gate(
                            pr.get("pattern_hash", ""), raw_side,
                            raw_conviction, market_open, strategy, regime)
                    except Exception as gate_exc:
                        allowed, why, evidence = (
                            False, "paper exploration gate unavailable: %s" % gate_exc,
                            {"paper_exploration_eligible": False,
                             "error": str(gate_exc)},
                        )
                    signal_dict["paper_exploration_evidence"] = evidence
                    signal_dict["paper_exploration_allowed"] = allowed
                    if allowed:
                        exploration_signal = trade_mod.TradeSignal(
                            symbol=symbol,
                            action="buy",
                            conviction=raw_conviction,
                            quantity=1,
                            source="shadow-promoted-paper-exploration",
                            stop_loss_pct=STOP_LOSS_PCT,
                            take_profit_pct=TAKE_PROFIT_PCT,
                            reason=(
                                f"Cycle #{cycle_num} promoted shadow pattern "
                                f"{pr.get('pattern_hash')} ({strategy}, {regime})"
                            ),
                        )
                        exploration_result = self.trading.execute(
                            exploration_signal,
                            overnight_risk=self.carries_overnight_risk())
                        signal_dict["executed_paper_exploration"] = exploration_result.success
                        if exploration_result.success:
                            signal_dict["executed"] = True
                            signal_dict["tier"] = "shadow_promoted_exploration"
                        signal_dict["paper_exploration_order_id"] = exploration_result.order_id
                        signal_dict["paper_exploration_status"] = exploration_result.status.value
                        if exploration_result.success and exploration_result.filled_price:
                            self.state.paper_exploration_trades_today += 1
                            self.state.paper_exploration_total_trades += 1
                            try:
                                self._record_filled_pattern(
                                    symbol, "buy", raw_conviction, rsi_value,
                                    exploration_result, signal_dict, strategy, regime,
                                    "shadow_promoted_exploration")
                            except Exception as record_exc:
                                logger.error(
                                    "PAPER EXPLORATION [%s] filled but pattern "
                                    "tracking failed: %s", symbol, record_exc)
                            logger.warning(
                                "PAPER EXPLORATION [%s]: bought exactly one share "
                                "after shadow promotion", symbol)
                    else:
                        signal_dict["paper_exploration_blocked"] = why
                        self._record_refusal("paper exploration evidence not ready", symbol)

                # Tier 2: small PAPER-only trade in the RSI grey zone, purely
                # to give the threshold advisory real comparison data. Never
                # runs if Tier-1 already claimed this symbol this cycle.
                if not signal_dict.get("executed") and tier2_side:
                    allowed, why = self._tier2_exploration_gate(market_open)
                    signal_dict["tier2_exploration_allowed"] = allowed
                    if allowed:
                        tier2_conviction = _pmod.mean_reversion_conviction(rsi_value)
                        _orig_risk = self.trading.risk_per_trade
                        self.trading.risk_per_trade = TIER_1_RISK_PER_TRADE * TIER_2_RISK_FACTOR
                        try:
                            tier2_signal = trade_mod.TradeSignal(
                                symbol=symbol,
                                action=tier2_side,
                                conviction=tier2_conviction,
                                source="tier2-rsi-threshold-exploration",
                                stop_loss_pct=STOP_LOSS_PCT,
                                take_profit_pct=TAKE_PROFIT_PCT,
                                reason=(
                                    f"Cycle #{cycle_num} tier-2 RSI grey-zone "
                                    f"exploration (rsi={rsi_value:.1f})"
                                ),
                            )
                            tier2_result = self.trading.execute(
                                tier2_signal, overnight_risk=self.carries_overnight_risk())
                        finally:
                            self.trading.risk_per_trade = _orig_risk
                        signal_dict["tier2_exploration_side"] = tier2_side
                        signal_dict["executed_tier2_exploration"] = tier2_result.success
                        if tier2_result.success:
                            signal_dict["executed"] = True
                            signal_dict["tier"] = "exploration"
                        signal_dict["tier2_exploration_order_id"] = tier2_result.order_id
                        signal_dict["tier2_exploration_status"] = tier2_result.status.value
                        if tier2_result.success and tier2_result.filled_price:
                            self.state.tier_2_trades_today += 1
                            self.state.tier_2_total_trades += 1
                            try:
                                self._record_filled_pattern(
                                    symbol, tier2_side, tier2_conviction, rsi_value,
                                    tier2_result, signal_dict, strategy, regime,
                                    "exploration")
                            except Exception as record_exc:
                                logger.error(
                                    "TIER 2 EXPLORATION [%s] filled but pattern "
                                    "tracking failed: %s", symbol, record_exc)
                            logger.warning(
                                "TIER 2 EXPLORATION [%s]: %s in RSI grey zone (rsi=%.1f)",
                                symbol, tier2_side.upper(), rsi_value)
                    else:
                        signal_dict["tier2_exploration_blocked"] = why
                        self._record_refusal("tier 2 exploration not authorized", symbol)

                result.setdefault("trade_signals", []).append(signal_dict)

        except Exception as e:
            logger.error("Step 4/5 FAILED: %s", e)
            result["steps"]["trading"] = {"status": "error", "error": str(e)}
            self.state.errors.append(f"Cycle {cycle_num} trading: {e}")

        # ---- Reference price failure tracking ----
        # This block sits AFTER the news-degraded early return (Step 1 guard)
        # and the trading exception handler.  On news-degraded cycles neither
        # flag is touched — the counter is preserved (intentional: a broken
        # data source is not a successful cycle).
        #
        # Three-way tracking:
        #   succeeded → reset counter (at least one good fetch)
        #   failed   → increment counter
        #   neither  → leave unchanged (e.g. news-degraded skip, no trades)
        if self.trading.ref_price_succeeded:
            self.state.consecutive_ref_price_failures = 0
        elif self.trading.ref_price_failed:
            self.state.consecutive_ref_price_failures += 1

        if self.state.consecutive_ref_price_failures >= 3:
            logger.critical(
                "🔴 Reference price unavailable for %d consecutive cycles — "
                "Alpaca data feed may be down",
                self.state.consecutive_ref_price_failures,
            )

        self._finalize_cycle(cycle_start, result)
        return result


    def _generate_tier2_threshold_advisory(self) -> None:
        """Generate a Tier-2 RSI threshold *recommendation* report.

        This is advisory only. It compares paper-exploration ('tier=exploration')
        outcomes against the Tier-1 baseline and writes recommended threshold
        nudges to tuned_thresholds.json, explicitly tagged
        ``recommendation_only: True``. It never writes to
        ``patterns.RSI_OVERSOLD`` / ``RSI_OVERBOUGHT`` or any other live
        trading constant. Applying a recommendation is a separate, deliberate
        operator action (edit the threshold in patterns.py, validate in paper
        mode, keep a rollback path) -- consistent with this codebase's rule
        that only the operator's own action may change persisted trading
        state, not an automated evaluation loop.

        NOTE: as of this writing nothing calls this method, and nothing tags
        pattern_memory rows with tier='exploration' or increments
        ``tier_2_total_trades`` -- the whole Tier-2 concept this advisory
        reads from is currently dormant. It is left uncalled here rather than
        wired into the pipeline, since doing that would mean also building
        the trade-tagging/counting side in the live execution path, which is
        a separate, larger change.
        """
        if self.state.tier_2_total_trades < 25 * (self.state.tier_2_eval_cycle + 1):
            return
        try:
            import json
            import time as _time
            import hashlib
            from pathlib import Path
            thresholds_path = Path(DATA_DIR) / "tuned_thresholds.json"
            audit_path = Path(DATA_DIR) / "tuning_audit.json"
            if not thresholds_path.exists():
                return
            with open(thresholds_path) as f:
                thresholds = json.load(f)

            pmod = _import_patterns()

            # Query Tier 2 (paper-exploration) completed trades from pattern_memory
            try:
                conn = self.patterns.db._connect()
                rows = conn.execute(
                    "SELECT rsi_value, outcome, timestamp FROM pattern_memory "
                    "WHERE tier='exploration' AND outcome != 'pending'"
                ).fetchall()
            except Exception:
                rows = []
            if not rows:
                return

            _timestamps = [r[2] for r in rows if r[2] is not None]
            data_window = {
                "start": min(_timestamps) if _timestamps else None,
                "end": max(_timestamps) if _timestamps else None,
            }

            # Group by RSI bucket (3-point buckets)
            buckets = {}
            for row in rows:
                rsi = row[0] if row[0] else 50
                outcome = row[1] if row[1] else "loss"
                bucket = int(rsi // 3) * 3
                if bucket not in buckets:
                    buckets[bucket] = {"wins": 0, "total": 0}
                buckets[bucket]["total"] += 1
                buckets[bucket]["wins"] += 1 if outcome == "win" else 0
            # Query Tier 1 baseline win rate
            try:
                conn = self.patterns.db._connect()
                tier1_rows = conn.execute(
                    "SELECT outcome FROM pattern_memory WHERE tier='signal' AND outcome != 'pending'"
                ).fetchall()
            except Exception:
                tier1_rows = []
            tier1_wins = sum(1 for r in tier1_rows if r[0] == "win")
            tier1_total = len(tier1_rows)
            tier1_win_rate = tier1_wins / tier1_total if tier1_total > 0 else 0.5

            # The live config this recommendation was generated against. If
            # the live thresholds change later, this hash shows a stored
            # recommendation was computed against a now-superseded baseline.
            config_hash = hashlib.sha256(
                (
                    f"RSI_OVERSOLD={pmod.RSI_OVERSOLD}|"
                    f"RSI_OVERBOUGHT={pmod.RSI_OVERBOUGHT}|"
                    f"ADX_TREND_MIN={pmod.ADX_TREND_MIN}|"
                    f"ADX_RANGE_MAX={pmod.ADX_RANGE_MAX}"
                ).encode()
            ).hexdigest()[:16]

            # Evaluate each bucket. Oversold (bucket < 50) and overbought
            # (bucket >= 50) are mirror images of each other around RSI 50 --
            # the original version of this method only ever named results
            # "tier_1_rsi_oversold"/"tier_2_rsi_oversold" regardless of which
            # side a bucket fell on, which would have mislabeled every
            # overbought recommendation once real overbought data existed.
            # Promote/tighten step sizes and bounds are mirrored unchanged
            # from the oversold side (100 - value), not independently
            # re-derived -- that directional logic predates any real data and
            # is reviewed by a human before ever being applied, per the
            # docstring above.
            audit_entries = []

            def _evaluate_bucket(bucket: int, stats: dict, oversold: bool) -> None:
                if stats["total"] < 3:
                    return
                wr = stats["wins"] / stats["total"]
                ci_low, ci_high = pmod.PatternStats._wilson_bounds(stats["wins"], stats["total"])
                side = "oversold" if oversold else "overbought"
                if wr >= tier1_win_rate - 0.05:
                    param = f"tier_1_rsi_{side}"
                    default = 30.0 if oversold else 70.0
                    bound = 25.0 if oversold else 75.0
                    step = -1.0 if oversold else 1.0
                    direction = "promote"
                    reason = f"Tier 2 bucket RSI {bucket}-{bucket+3} within 5% of Tier 1 baseline"
                elif wr <= tier1_win_rate - 0.10:
                    param = f"tier_2_rsi_{side}"
                    default = 35.0 if oversold else 65.0
                    bound = 40.0 if oversold else 60.0
                    step = 1.0 if oversold else -1.0
                    direction = "tighten"
                    reason = f"Tier 2 bucket RSI {bucket}-{bucket+3} underperforms Tier 1 by >10%"
                else:
                    return
                old_val = thresholds.get(param, default)
                new_val = max(bound, old_val + step) if step < 0 else min(bound, old_val + step)
                still_room = (old_val > bound) if step < 0 else (old_val < bound)
                if new_val == old_val or not still_room:
                    return
                thresholds[param] = new_val
                audit_entries.append({
                    "timestamp": _time.time(),
                    "parameter": param,
                    "old_value": old_val,
                    "new_value": new_val,
                    "direction": direction,
                    "sample_size": stats["total"],
                    "tier2_win_rate": round(wr, 4),
                    "tier2_win_rate_ci95": [round(ci_low, 4), round(ci_high, 4)],
                    "tier1_win_rate": round(tier1_win_rate, 4),
                    "reason": reason,
                    "recommendation_only": True,
                })
                logger.warning(
                    "THRESHOLD ADVISORY (recommendation only, live thresholds "
                    "unchanged): %s recommend %.1f -> %.1f "
                    "(T2 WR=%.3f [%.3f, %.3f] n=%d, T1 WR=%.3f)",
                    param, old_val, new_val, wr, ci_low, ci_high,
                    stats["total"], tier1_win_rate,
                )

            for bucket, stats in sorted(buckets.items()):
                _evaluate_bucket(bucket, stats, oversold=bucket < 50)

            thresholds["recommendation_only"] = True
            thresholds["updated_at"] = _time.time()
            thresholds["data_window"] = data_window
            thresholds["sample_size"] = len(rows)
            thresholds["config_hash"] = config_hash
            with open(thresholds_path, "w") as f:
                json.dump(thresholds, f, indent=2)
            if audit_entries:
                try:
                    with open(audit_path) as f:
                        audit_data = json.load(f)
                except Exception:
                    audit_data = []
                audit_data.extend(audit_entries)
                with open(audit_path, "w") as f:
                    json.dump(audit_data, f, indent=2)
            self.state.tier_2_eval_cycle += 1
            logger.info(
                "Threshold advisory cycle %d complete: recommendation generated, "
                "live thresholds unchanged (n=%d, window=%s..%s)",
                self.state.tier_2_eval_cycle, len(rows),
                data_window["start"], data_window["end"],
            )
            # _save_persisted_mode() was called here. It was harmless while
            # that function was a no-op stub; once implemented it would
            # persist whatever mode happened to be current -- including an
            # automated DAILY_LOSS_LIMIT demotion, which would overwrite the
            # pre-halt mode that day-rollover recovery reads back. Only
            # set_mode(), the operator's own action, may write that file.
        except Exception as e:
            logger.error("Threshold advisory generation FAILED: %s", e)


    def _finalize_cycle(self, start: float, result: dict) -> None:
        """Finalize a pipeline cycle with monitoring checks."""
        elapsed = round(time.time() - start, 3)
        result["elapsed_seconds"] = elapsed
        result["refusals"] = dict(self.state.latest_refusals or {})
        self.state.last_cycle_time = time.time()

        # ---- Comprehensive decision audit entry (one JSON line per cycle) ----
        # Captures the full reasoning chain so any trade — or non-trade — can be
        # reconstructed: inputs → sentiment → regime → patterns → portfolio →
        # blended conviction → decision.
        try:
            steps = result.get("steps", {})
            sent = steps.get("sentiment", {})
            trade_signals = result.get("trade_signals", [])

            # Derive the top-level decision for the cycle.
            executed = [s for s in trade_signals if s.get("executed")]
            if executed:
                decisions = sorted(
                    executed, key=lambda s: abs(s.get("conviction", 0)), reverse=True
                )
                top = decisions[0]
                decision = (top.get("side") or "none").upper()
            else:
                decision = "NONE"

            # Portfolio snapshot (best-effort — never block the cycle).
            try:
                acct = self.trading.broker.get_account()
                portfolio = {
                    "buying_power": acct.buying_power,
                    "equity": acct.equity,
                    "cash": acct.cash,
                    "portfolio_value": acct.portfolio_value,
                    "open_positions": len(acct.positions),
                    "positions": acct.positions,
                    "mode": "simulation" if self.trading.broker.is_simulating else "live",
                }
            except Exception as pe:
                portfolio = {"error": str(pe)}

            audit_record = {
                "cycle": result.get("cycle"),
                "mode": self.state.mode.value,
                "elapsed_seconds": elapsed,
                # Inputs: headlines + per-headline sentiment
                "headline_count": steps.get("news", {}).get("headline_count", 0),
                "headlines": result.get("audit_headlines", []),
                # Aggregate sentiment
                "aggregate_sentiment": sent.get("conviction"),
                "consensus": sent.get("consensus"),
                "volatility_signal": sent.get("volatility"),
                # Market regime
                "regime": steps.get("regime", {}),
                # Market-hours state (why execution may have been gated)
                "market_hours": steps.get("market_hours", self.state.market_hours),
                # Pattern engine results
                "patterns": steps.get("patterns", {}).get("evaluations", []),
                # Portfolio state
                "portfolio": portfolio,
                # Every candidate signal + its decision / skip reason
                "signals": trade_signals,
                # Any positions closed this cycle (stop/target)
                "closed_positions": result.get("closed_positions", []),
                # Final decision for the cycle
                "decision": decision,
                "errors": result.get("steps", {}).get("trading", {}).get("error"),
            }
            self.audit.log_cycle(audit_record)
        except Exception as e:
            logger.warning("Cycle audit log failed: %s", e)

        # Run monitoring checks
        try:
            sent_conv = result.get("steps", {}).get("sentiment", {}).get("conviction", 0.0)
            alert_result = self.alerts.check_cycle(
                sentiment_conviction=sent_conv,
                consensus=result.get("steps", {}).get("sentiment", {}).get("consensus", "neutral"),
                errors=self.state.errors,
                cycle_count=self.state.cycle_count,
            )
            result["monitoring"] = alert_result

            # Send pending notifications to the lead
            mod = _import_monitoring()
            sent_notifs = mod.send_pending_notifications()
            if sent_notifs:
                logger.info("Sent %d notifications to lead", len(sent_notifs))
        except Exception as e:
            logger.warning("Monitoring check failed: %s", e)

        # Cheap no-op most cycles: the advisory only does anything once 25
        # more Tier 2 trades have landed since its last run.
        try:
            self._generate_tier2_threshold_advisory()
        except Exception as e:
            logger.warning("Tier 2 threshold advisory failed: %s", e)

                # ---- Drawdown safety checks ----
        try:
            # Use current equity from account, falling back to starting equity
            try:
                acct = self.trading.broker.get_account()
                equity = float(acct.equity) if hasattr(acct, 'equity') else (self.state.daily_starting_equity or 0.0)
            except Exception:
                equity = self.state.daily_starting_equity or 0.0
            # Guard: reject equity <= $1.00 — broker glitch, not a real loss.
            # Three consecutive failures escalate to CRITICAL but never kill.
            if equity <= 1.0:
                self.state.equity_read_failures += 1
                if self.state.equity_read_failures >= 3:
                    logger.critical(
                        "Drawdown check: equity=%.2f invalid (<=$1.00) for %d consecutive cycles — "
                        "broker may be unavailable. No kill, retrying next cycle.",
                        equity, self.state.equity_read_failures,
                    )
                else:
                    logger.warning(
                        "Drawdown check: equity=%.2f is invalid (<=$1.00) — skipping "
                        "(failure %d/3)", equity, self.state.equity_read_failures,
                    )
                return

            # Successful equity read — reset failure counter
            self.state.equity_read_failures = 0
            if equity > self.state.peak_equity:
                self.state.peak_equity = equity
            current_dd = 0.0
            if self.state.peak_equity > 0:
                current_dd = (self.state.peak_equity - equity) / self.state.peak_equity * 100.0
            self.state.max_drawdown_pct = max(self.state.max_drawdown_pct, current_dd)
            _kill_dd_pct = _import_patterns().DRAWDOWN_KILL_PCT * 100.0
            _halve_dd_pct = _import_patterns().DRAWDOWN_HALVE_PCT * 100.0
            if current_dd >= _kill_dd_pct:
                self.state.killed = True
                self.state.drawdown_killed = True
                self._write_killed_state_file()
                logger.warning(
                    "KILLED flag set (trigger: drawdown %.2f%% >= %.0f%%) — persisted to %s",
                    current_dd, _kill_dd_pct, KILLED_STATE_FILE,
                )
                logger.critical(
                    "DRAWDOWN KILL: %.2f%% drawdown exceeds %.0f%% limit -- killed flag set",
                    current_dd, _kill_dd_pct,
                )
            elif current_dd >= _halve_dd_pct:
                self.state.position_size_multiplier = 0.5
                logger.warning(
                    "DRAWDOWN HALVING: %.2f%% drawdown exceeds %.0f%% -- position sizes halved",
                    current_dd, _halve_dd_pct,
                )
            else:
                self.state.position_size_multiplier = 1.0
            # Persist drawdown state to alerts DB
            try:
                from alert_db import insert_alert
                import json
                state = self._build_drawdown_state_dict()
                insert_alert("DRAWDOWN_STATE", json.dumps(state), "info")
            except Exception:
                pass

            # ---- File-based drawdown reset check ----
            reset_path = os.path.join(DATA_DIR, "RESET_DRAWDOWN")
            if os.path.exists(reset_path):
                self.state.killed = False
                self.state.health_failed_this_session = False
                try:
                    if os.path.exists(KILLED_STATE_FILE):
                        os.remove(KILLED_STATE_FILE)
                except Exception:
                    pass
                if self.state.drawdown_killed:
                    try:
                        acct = self.trading.broker.get_account()
                        current_equity = float(acct.equity) if hasattr(acct, 'equity') else 0.0
                    except Exception:
                        current_equity = 0.0
                    self.state.drawdown_killed = False
                    self.state.peak_equity = current_equity
                    self.state.max_drawdown_pct = 0.0
                    self.state.position_size_multiplier = 1.0
                    logger.critical(
                        "DRAWDOWN KILL MANUALLY RESET by operator -- "
                        "new peak_equity=%.2f", current_equity,
                    )
                    try:
                        from alert_db import insert_alert
                        import json
                        state = self._build_drawdown_state_dict()
                        state.update({
                            "peak_equity": round(current_equity, 2),
                            "max_drawdown_pct": 0.0,
                            "position_size_multiplier": 1.0,
                            "drawdown_killed": False,
                            "killed": False,
                            "consecutive_ref_price_failures": 0,
                        })
                        insert_alert("DRAWDOWN_STATE", json.dumps(state), "info")
                    except Exception:
                        pass
                else:
                    logger.warning(
                        "RESET_DRAWDOWN file found but drawdown kill is not active -- ignoring"
                    )
                # Re-read the operator mode file so that any mode change the
                # operator made alongside the RESET_DRAWDOWN takes effect.
                # (The API /reset path does this already; the file-based path
                #  was missing it — documented in MODE_PRECEDENCE.md.)
                self._load_persisted_mode()
                try:
                    os.remove(reset_path)
                except Exception:
                    pass
        except Exception:
            pass
        if self.state.is_autonomous:
            logger.info(
                "Cycle #%d complete in %.1fs (autonomous mode) -- equity=%.2f, peak=%.2f, drawdown=%.2f%%",
                self.state.cycle_count, elapsed, equity or 0,
                self.state.peak_equity, current_dd,
            )
        else:
            logger.info(
                "Cycle #%d complete in %.1fs (manual mode -- no trades executed)",
                self.state.cycle_count, elapsed,
            )   # ------------------------------------------------------------------
    # Drawdown state helpers
    # ------------------------------------------------------------------
    def _build_drawdown_state_dict(self) -> dict:
        """Return a dict with all drawdown-state keys for persistence.

        Single source of truth — all DRAWDOWN_STATE writes call this.
        Adding a new field here propagates everywhere automatically,
        eliminating the schema-drift risk that caused the July 12 false kill.
        """
        return {
            "peak_equity": round(self.state.peak_equity, 2),
            "max_drawdown_pct": round(self.state.max_drawdown_pct, 2),
            "position_size_multiplier": self.state.position_size_multiplier,
            "drawdown_killed": self.state.drawdown_killed,
            "killed": self.state.killed,
            "consecutive_ref_price_failures": getattr(self.state, "consecutive_ref_price_failures", 0),
            "tier_2_trades_today": self.state.tier_2_trades_today,
            "tier_2_total_trades": self.state.tier_2_total_trades,
            "tier_2_eval_cycle": self.state.tier_2_eval_cycle,
            "signal_trade_count": self.state.signal_trade_count,
            "paper_exploration_trades_today": self.state.paper_exploration_trades_today,
            "paper_exploration_total_trades": self.state.paper_exploration_total_trades,
        }

    # ------------------------------------------------------------------
    # On-demand evaluation
    # ------------------------------------------------------------------
    def evaluate_now(self, symbol: str = "SPY") -> dict:
        """Run an on-demand evaluation cycle for a specific symbol."""
        try:
            headlines = self.news.fetch_headlines(MAX_HEADLINES)
            sent = self.sentiment.analyze(headlines)
            agg_conv = sent.aggregate_conviction

            # Use the same real, closed-bar inputs as the autonomous cycle.
            ohlc = self._fetch_ohlc(symbol, bars=INDICATOR_FETCH_BARS)
            _closes = ohlc["closes"] if ohlc else []
            pmod = _import_patterns()
            _ema_short = pmod.compute_ema(_closes, EMA_SHORT_PERIOD) if _closes else None
            _ema_long = pmod.compute_ema(_closes, EMA_LONG_PERIOD) if _closes else None
            _prev_short = pmod.compute_ema(_closes[:-1], EMA_SHORT_PERIOD) if _closes else None
            _prev_long = pmod.compute_ema(_closes[:-1], EMA_LONG_PERIOD) if _closes else None
            _rsi = pmod.compute_rsi(_closes) if _closes else None

            if _ema_short is None or _ema_long is None:
                logger.warning("evaluate_now: skipping %s — EMA unavailable", symbol)
                return {"symbol": symbol, "action": "skip", "reason": "EMA unavailable"}
            pattern_sig = self.patterns.evaluate(
                symbol=symbol,
                sentiment_score=agg_conv,
                conviction_score=agg_conv,
                rsi_value=_rsi,
                ema_short=_ema_short,
                ema_long=_ema_long,
                prev_ema_short=_prev_short,
                prev_ema_long=_prev_long,
            )

            return {
                "symbol": symbol,
                "headlines_analyzed": len(headlines),
                "sentiment_conviction": agg_conv,
                "sentiment_consensus": sent.consensus,
                "pattern": {
                    "action": pattern_sig.action,
                    "conviction": pattern_sig.conviction,
                    "historical_samples": pattern_sig.pattern_stats.count,
                    "win_rate": pattern_sig.pattern_stats.win_rate,
                    "reason": pattern_sig.reason,
                },
                "recommendation": (
                    "EXECUTE" if abs(agg_conv) >= self.high_conviction
                    else "MONITOR"
                ),
            }
        except Exception as e:
            logger.error("Evaluate failed: %s", e)
            return {"error": str(e), "symbol": symbol}

    def authorize_entry(self, source: str = "manual") -> tuple:
        """(allowed, reason) -- the safety gates every entry must clear.

        `POST /api/execute` used to call the trading engine directly, so none
        of the orchestrator's own gates applied. Measured before this existed:
        an order reached the engine with the kill switch engaged, with the
        daily loss limit hit, and with the operator mode set to STOPPED. The
        kill switch did not stop the API.

        Manual mode exists so a human can place a trade the strategy would not
        have chosen. It does not exist to override safety -- a human deciding
        to trade is not evidence that the daily loss limit was wrong. So MANUAL
        is allowed through here, and every kill, halt and stop is not.

        The engine applies its own gates underneath this one (ledger kill
        switch, exposure, cooldown, execution quality). Those are per-order
        facts; these are account-level halts, and nothing else checked them.
        """
        if self.state.killed:
            return False, "kill switch engaged"
        if self.state.daily_loss_hit:
            return False, "daily loss limit hit — halted for the day"
        if self.state.mode is OrchestratorMode.STOPPED:
            return False, "operator mode is STOPPED"
        if self.state.mode is OrchestratorMode.KILLED:
            return False, "operator mode is KILLED"
        if self.state.mode is OrchestratorMode.DAILY_LOSS_LIMIT:
            return False, "daily loss limit mode active"
        if self.state.startup_recovery_blocked:
            return False, "startup recovery incomplete"
        if self.state.health_failed_this_session:
            return False, "pre-market health check failed"
        preflight = getattr(self.state, "preflight", None) or {}
        if preflight and not preflight.get("ok", True):
            return False, ("preflight failed: %s"
                           % "; ".join(preflight.get("blocking", []) or []))
        try:
            if not self.clock.is_open():
                return False, "market is closed"
        except Exception as exc:
            # Session truth unavailable fails closed, matching the rest of the
            # system: an unknown market state is not an open one.
            return False, "market session unavailable: %s" % exc
        return True, "authorized (%s)" % source

    def execute_signal(self, symbol: str, conviction: float) -> dict:
        """Manually execute a trade signal, subject to the same safety gates."""
        allowed, reason = self.authorize_entry("manual")
        if not allowed:
            logger.warning("Manual entry refused for %s: %s", symbol, reason)
            try:
                if getattr(self.trading, "journal", None):
                    self.trading.journal.blocked(
                        symbol, "manual", blocker="manual entry refused",
                        detail=reason, inputs={"conviction": conviction,
                                               "source": "api"})
            except Exception:
                pass
            return {"success": False, "symbol": symbol,
                    "error": "Entry refused: %s" % reason, "refused": True}
        result = self.trading.evaluate_and_execute(
            symbol=symbol,
            sentiment_conviction=conviction,
        )
        return result.dict()

    # ------------------------------------------------------------------
    # Data accessors for API
    # ------------------------------------------------------------------
    def get_decisions(self, limit: int = 40) -> dict:
        """Recent decision-journal entries, newest last.

        Exposed over HTTP because refusals are the most informative signal a
        remote operator has. "No trades today" is ambiguous — it is either the
        confidence gate correctly declining noise, or a broken signal path.
        Refusals WITH REASONS distinguish the two; an empty journal means
        nothing ever reached a gate.
        """
        try:
            entries = self.trading.journal.read(limit=limit)
        except Exception as exc:
            return {"error": "journal unavailable: %s" % exc, "entries": []}
        summary: Dict[str, int] = {}
        for entry in entries:
            key = entry.get("blocker") or entry.get("event") or "unknown"
            summary[key] = summary.get(key, 0) + 1
        return {"entries": entries, "counts": summary, "returned": len(entries)}

    def get_state(self) -> dict:
        """Return current orchestrator state."""
        return self.state.to_dict()

    def get_shadow_status(self) -> dict:
        """Read-only progress toward evidence-gated paper exploration."""
        try:
            status = self.shadow.status()
            status.update({
                "minimum_completed_per_pattern": SHADOW_PROMOTION_MIN_TRADES,
                "minimum_distinct_days_per_pattern": SHADOW_PROMOTION_MIN_DAYS,
                "paper_only": True,
                "quantity_cap": 1,
                "long_only": True,
            })
            return status
        except Exception as exc:
            return {"error": str(exc), "paper_only": True, "quantity_cap": 1}

    def get_sentiment_data(self) -> dict:
        """Return latest sentiment analysis results."""
        if self.state.last_sentiment_result:
            return self.state.last_sentiment_result
        # Return empty state
        return {
            "conviction_score": 0.0,
            "consensus": "neutral",
            "volatility_signal": 0.0,
            "headline_count": 0,
            "headlines": [],
            "note": "No data yet. Run a pipeline cycle first.",
        }

    def get_pattern_data(self) -> dict:
        """Return pattern memory summary."""
        try:
            summary = self.patterns.summary()
            latest = self.state.last_pattern_result or []
            return {
                "summary": summary,
                "latest_evaluations": latest,
            }
        except Exception as e:
            return {"error": str(e)}

    def get_trade_history(self) -> List[dict]:
        """Return recent trade history."""
        try:
            return self.trading.get_recent_trades(20)
        except Exception:
            return []


    def get_latest_reconciliation(self) -> dict:
        """Return the latest daily reconciliation report."""
        recon_file = os.path.join(DATA_DIR, "reconciliation_latest.json")
        if os.path.exists(recon_file):
            try:
                with open(recon_file, "r") as f:
                    import json
                    return json.load(f)
            except Exception as e:
                return {"status": "error", "error": f"Failed to read file: {e}"}
        return {"status": "pending", "message": "No reconciliation report has been generated yet today."}

    def get_pre_market_health(self) -> dict:
        """Return the latest pre-market health check report."""
        health_file = os.path.join(DATA_DIR, "health_pre_market.json")
        if os.path.exists(health_file):
            try:
                with open(health_file, "r") as f:
                    import json
                    return json.load(f)
            except Exception as e:
                return {"status": "error", "error": f"Failed to read file: {e}"}
        return {"status": "pending", "message": "No pre-market health check has been performed yet today."}

    def daily_self_review(self) -> dict:
        """Run the checks a human would otherwise have to remember, and alert.

        The decision journal and the forward test already answer "is execution
        degrading" and "is this ready for real money" -- but only if somebody
        runs them. An automatic bot cannot depend on that, so the same analysis
        runs after each session and anything actionable is raised as an alert
        rather than left sitting in a file.

        Never raises: a review that can break the trading loop is worse than
        no review.
        """
        summary = {"findings": [], "alerts": 0}
        try:
            from monitoring import AlertManager
            alerts = AlertManager()
        except Exception:
            alerts = None

        def raise_alert(kind, severity, message):
            summary["findings"].append(message)
            if alerts is not None:
                try:
                    alerts._create_alert(kind, severity, message)
                    summary["alerts"] += 1
                except Exception:
                    pass

        # --- execution quality, per symbol ------------------------------
        try:
            from decision_log import DecisionLog
            journal = DecisionLog()
            quality = journal.execution_quality(TAKE_PROFIT_PCT * 100.0)
            for symbol, reason in (quality.get("reasons") or {}).items():
                raise_alert("execution_cost", "critical",
                            "%s is too expensive to trade: %s" % (symbol, reason))
            summary["execution_quality"] = quality

            report = journal.postmortem(
                stop_loss_pct=STOP_LOSS_PCT * 100.0,
                take_profit_pct=TAKE_PROFIT_PCT * 100.0)
            summary["postmortem"] = report
            for finding in report.get("findings", []):
                # These are diagnoses, not faults: worth surfacing, not
                # worth waking anyone up.
                raise_alert("strategy_review", "info", finding)
        except Exception as exc:
            logger.warning("Daily review: journal analysis failed: %s", exc)

        # --- readiness for real money -----------------------------------
        try:
            from forward_test import ForwardTest
            forward = ForwardTest().evaluate()
            summary["forward"] = forward.to_dict()
            was_ready = self.state.forward_ready
            self.state.forward_ready = forward.ready
            if forward.ready and not was_ready:
                raise_alert(
                    "forward_ready", "critical",
                    "Forward test gate PASSED: %d trades over %d days, win "
                    "rate %.1f%% (95%% CI %.1f-%.1f%%), expectancy %+.4f%%. "
                    "This is evidence of edge, not proof -- paper fills are "
                    "optimistic."
                    % (forward.trades, forward.trading_days,
                       forward.win_rate * 100, forward.win_rate_low * 100,
                       forward.win_rate_high * 100, forward.expectancy_pct))
            elif was_ready and not forward.ready:
                raise_alert(
                    "forward_ready", "critical",
                    "Forward test gate NO LONGER met: %s"
                    % "; ".join(forward.blockers))
        except Exception as exc:
            logger.warning("Daily review: forward test failed: %s", exc)

        # --- positions running without a working stop -------------------
        unprotected = self.unprotected_positions()
        if unprotected:
            raise_alert(
                "unprotected_position", "critical",
                "positions could not be evaluated against their stops: %s"
                % ", ".join("%s (%d checks)" % (s, n)
                            for s, n in sorted(unprotected.items())))

        # --- the order ledger should settle each day --------------------
        try:
            truth = self.trading.position_truth
            if truth is not None:
                stats = truth.safety.stats()
                summary["ledger"] = stats
                if stats.get("unresolved"):
                    raise_alert(
                        "ledger_unresolved", "warning",
                        "%d order(s) are still unresolved after the close — "
                        "a reserved order means a crash mid-submit, a residual "
                        "one means a position did not actually flatten"
                        % stats["unresolved"])
                # Keep the hot table proportional to OPEN orders.
                truth.safety.prune(older_than_days=7.0)
        except Exception as exc:
            logger.warning("Daily review: ledger check failed: %s", exc)

        logger.info("Daily self-review complete: %d finding(s), %d alert(s)",
                    len(summary["findings"]), summary["alerts"])
        self.state.last_review = summary
        return summary

    def run_reconciliation(self) -> dict:
        """Execute full post-market reconciliation logic."""
        import json
        logger.info("Executing post-market reconciliation...")
        # The checks a human would otherwise have to remember to run.
        try:
            self.daily_self_review()
        except Exception as exc:
            logger.error("Daily self-review failed (non-fatal): %s", exc)
        symbols = list(TRADING_SYMBOLS)
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        
        # 1. Fetch complete daily bars & store them
        bars_fetched = {}
        for sym in symbols:
            ohlc = self._fetch_ohlc(sym, bars=INDICATOR_REQUIRED_BARS)
            if ohlc:
                bars_fetched[sym] = ohlc
                # Store latest daily bar into the pattern memory SQLite
                try:
                    if (not ohlc.get("bar_dates") or ohlc["bar_dates"][-1] is None
                            or ohlc["opens"][-1] is None or ohlc["volumes"][-1] is None):
                        logger.warning("Reconciliation skipped %s: missing real timestamp/open/volume", sym)
                        continue
                    # store_daily_bar validates with date.fromisoformat, which
                    # accepts YYYY-MM-DD only. This passed a full timestamp --
                    # "2026-08-14T19:30:00+00:00" -- so EVERY bar was rejected
                    # as malformed and daily_bars never updated after the
                    # one-time backfill. The backfill path two thousand lines
                    # up already normalises to a date; this one did not.
                    _bar_ts = ohlc["bar_dates"][-1]
                    _bar_date = (_bar_ts.astimezone(timezone.utc).date()
                                 if getattr(_bar_ts, "tzinfo", None)
                                 else _bar_ts.date())
                    self.patterns.db.store_daily_bar(
                        symbol=sym,
                        date_str=str(_bar_date),
                        open_p=ohlc["opens"][-1],
                        high=ohlc["highs"][-1],
                        low=ohlc["lows"][-1],
                        close=ohlc["closes"][-1],
                        volume=ohlc["volumes"][-1]
                    )
                except Exception as de:
                    logger.error(f"Failed to store daily bar for {sym}: {de}")
            else:
                logger.warning("Reconciliation OHLC unavailable for %s; skipping bar update", sym)
        # 2. Recompute indicators and classification on actual closed data
        recomputed = {}
        for sym in symbols:
            db_bars = self.patterns.db.get_recent_daily_bars(sym, limit=40)
            if len(db_bars) >= 15:
                closes = [b["close"] for b in db_bars]
                highs = [b["high"] for b in db_bars]
                lows = [b["low"] for b in db_bars]
                
                # Standalone helper imports
                pmod = _import_patterns()
                rsi_val = pmod.compute_rsi(closes)
                adx_val = pmod.compute_adx(highs, lows, closes)
                regime = pmod.classify_regime(adx_val)
                
                recomputed[sym] = {
                    "rsi": rsi_val,
                    "adx": adx_val,
                    "regime": regime
                }
                logger.info(f"Recomputed {sym} indicators: RSI={rsi_val}, ADX={adx_val}, Regime={regime}")
        
        # 3. Reconcile fills vs expectations & track slippage
        # Pull any closed trades from today's pattern_memory
        conn = self.patterns.db._connect()
        trades_today = conn.execute(
            """SELECT symbol, entry_price, exit_price, profit_pct, conviction_score, outcome 
               FROM pattern_memory 
               WHERE timestamp >= ?""",
            (time.time() - 86400,)
        ).fetchall()
        
        reconciled_trades = []
        total_slippage = 0.0
        expected_pnl = 0.0
        actual_pnl = 0.0
        
        for t in trades_today:
            # Under a backtest model we expect perfect signal entry price vs actual fill
            expect_entry = t["entry_price"]
            actual_entry = t["entry_price"] # simplified for paper
            slippage = abs(actual_entry - expect_entry)
            total_slippage += slippage
            
            reconciled_trades.append({
                "symbol": t["symbol"],
                "expect_entry": expect_entry,
                "actual_entry": actual_entry,
                "slippage": slippage,
                "profit_pct": t["profit_pct"],
                "outcome": t["outcome"]
            })
            
        report = {
            "timestamp": time.time(),
            "date": today_str,
            "recomputed_indicators": recomputed,
            "trades_reconciled": reconciled_trades,
            "total_slippage": round(total_slippage, 4),
            "expected_pnl": expected_pnl,
            "actual_pnl": actual_pnl,
            "status": "success"
        }
        
        # Expose via file and logs
        os.makedirs(DATA_DIR, exist_ok=True)
        _write_json_atomic(
            os.path.join(DATA_DIR, "reconciliation_latest.json"), report,
            indent=2)

        logger.info(f"Daily Reconciliation complete. Total slippage={total_slippage}")

        # ---- Overnight Risk Snapshot ----
        try:
            overnight = self._compute_overnight_risk()
            report["overnight_risk"] = overnight
        except Exception as e:
            logger.warning("Overnight risk computation failed: %s", e)
            report["overnight_risk"] = {"error": str(e)}

        return report


    def _compute_overnight_risk(self) -> dict:
        """
        Compute an overnight risk snapshot: if all positions gap 3% against
        the bot overnight, what's the dollar loss?

        Returns:
            dict with keys: net_exposure, gap_risk_dollars, gap_risk_pct,
            margin_headroom, concentration_pct, largest_position,
            worst_case_line, positions, equity
        """
        import json
        result = {
            "timestamp": time.time(),
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "positions": [],
            "net_exposure": 0.0,
            "gap_risk_dollars": 0.0,
            "gap_risk_pct": 0.0,
            "margin_headroom": 0.0,
            "concentration_pct": 0.0,
            "largest_position": None,
            "equity": 0.0,
            "worst_case_line": "",
        }

        try:
            acct = self.trading.broker.get_account()
            equity = float(acct.equity)
            result["equity"] = equity
        except Exception as e:
            result["equity_error"] = str(e)
            equity = 0.0

        # Get active positions from patterns DB
        active_positions = []
        try:
            active_positions = self.patterns.db.get_active_positions()
        except Exception as e:
            logger.warning("Could not fetch active positions: %s", e)

        if not active_positions:
            result["worst_case_line"] = (
                f"No open positions. Risk: $0 (0% of equity). "
                f"Equity: ${equity:,.2f}"
            )
            # Write to file
            try:
                _write_json_atomic(
                    os.path.join(DATA_DIR, "overnight_risk_latest.json"),
                    result, indent=2, default=str)
            except Exception:
                pass
            return result

        total_long = 0.0
        total_short = 0.0
        max_position_value = 0.0
        max_position_symbol = None

        for pos in active_positions:
            symbol = pos["symbol"]
            side = pos["side"]
            entry_price = float(pos["entry_price"])
            quantity = int(pos["quantity"])

            # Get current market value
            current_price = entry_price  # fallback
            try:
                ref = self.trading._get_reference_price(symbol)
                if ref and ref > 0:
                    current_price = ref
            except Exception:
                pass

            position_value = current_price * quantity
            pos_record = {
                "symbol": symbol,
                "side": side,
                "qty": quantity,
                "entry_price": entry_price,
                "current_price": current_price,
                "market_value": round(position_value, 2),
            }

            if side == "buy":
                total_long += position_value
                gap_loss = position_value * 0.03
            else:
                total_short += position_value
                gap_loss = position_value * 0.03

            pos_record["gap_loss_3pct"] = round(gap_loss, 2)
            result["positions"].append(pos_record)

            if position_value > max_position_value:
                max_position_value = position_value
                max_position_symbol = symbol

        # Net exposure
        net_exposure = total_long - total_short
        result["net_exposure"] = round(net_exposure, 2)

        # Correlated gap risk: all positions gap 3% against us
        total_gap_loss = total_long * 0.03 + total_short * 0.03
        result["gap_risk_dollars"] = round(total_gap_loss, 2)
        result["gap_risk_pct"] = round(
            (total_gap_loss / equity * 100) if equity > 0 else 0, 2
        )

        # Concentration risk
        if equity > 0:
            result["concentration_pct"] = round(
                max_position_value / equity * 100, 2
            )
        result["largest_position"] = max_position_symbol

        # Margin headroom (best-effort from Alpaca)
        try:
            if hasattr(self.trading.broker, 'trading_client'):
                acct_obj = self.trading.broker.trading_client.get_account()
                long_val = float(getattr(acct_obj, 'long_market_value', 0))
                short_val = float(getattr(acct_obj, 'short_market_value', 0))
                maintenance = float(getattr(acct_obj, 'maintenance_margin_requirement', 0))
                result["margin_headroom"] = round(equity - maintenance, 2)
                result["margin_used"] = round(maintenance, 2)
            else:
                result["margin_headroom"] = round(equity * 0.5, 2)
                result["margin_used"] = round(equity * 0.5, 2)
        except Exception as e:
            result["margin_headroom"] = round(equity * 0.5, 2)
            result["margin_used"] = round(equity * 0.5, 2)
            result["margin_error"] = str(e)

        # Worst-case line
        result["worst_case_line"] = (
            f"Worst-case overnight gap loss: ${result['gap_risk_dollars']:,.2f} "
            f"({result['gap_risk_pct']:.1f}% of equity). "
            f"Margin headroom: ${result['margin_headroom']:,.2f}. "
            f"Concentration: {max_position_symbol} at {result['concentration_pct']:.1f}%"
        )

        logger.info("Overnight risk: %s", result["worst_case_line"])

        # Write to file
        try:
            _write_json_atomic(
                os.path.join(DATA_DIR, "overnight_risk_latest.json"),
                result, indent=2, default=str)
        except Exception as e:
            logger.warning("Failed to write overnight risk file: %s", e)

        return result

    def get_overnight_risk(self) -> dict:
        """Return the latest overnight risk snapshot."""
        risk_file = os.path.join(DATA_DIR, "overnight_risk_latest.json")
        if os.path.exists(risk_file):
            try:
                import json
                with open(risk_file) as f:
                    return json.load(f)
            except Exception as e:
                return {"status": "error", "error": f"Failed to read file: {e}"}
        return {
            "status": "pending",
            "message": "No overnight risk snapshot has been computed yet. "
                       "Run reconciliation after market close.",
        }

    def get_backfill_status(self) -> dict:
        """Return the latest historical data backfill status."""
        status_file = os.path.join(DATA_DIR, "backfill_status.json")
        if os.path.exists(status_file):
            try:
                import json
                with open(status_file) as f:
                    return json.load(f)
            except Exception as e:
                return {"status": "error", "error": f"Failed to read file: {e}"}
        return {
            "status": "pending",
            "message": "No backfill has been performed yet. "
                       "The weekend pipeline cycle or startup will trigger one.",
        }

    def run_pre_market_health(self, market_hours: Optional[dict] = None) -> dict:
        """Execute pre-market health checks & housekeeping.

        ``market_hours`` is the snapshot returned by the cycle's clock check.
        Passing it explicitly keeps the test-order gate aligned with the phase
        decision instead of relying on mutable state that another cycle could
        overwrite.
        """
        import json
        import subprocess
        logger.info("Executing pre-market health check and housekeeping...")
        checks = {}
        
        # 1. Rotate logs
        try:
            log_path = os.path.join(APP_ROOT, "orchestrator.log")
            if os.path.exists(log_path) and os.path.getsize(log_path) > 5 * 1024 * 1024:
                # Rotate log
                backup_path = f"{log_path}.{datetime.now(timezone.utc).strftime('%Y%m%d')}"
                os.rename(log_path, backup_path)
                with open(log_path, "w") as f:
                    f.write("")
                checks["log_rotation"] = "PASS"
            else:
                checks["log_rotation"] = "PASS (No rotation needed)"
        except Exception as e:
            checks["log_rotation"] = f"FAIL ({e})"
            
        # 2. Back up state
        try:
            backup_dir = os.path.join(DATA_DIR, "backups") + "/"
            os.makedirs(backup_dir, exist_ok=True)
            db_path = os.path.join(DATA_DIR, "patterns.db")

            if os.path.exists(db_path):
                date_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                backup_db = os.path.join(backup_dir, f"patterns_{date_str}.db")
                import shutil
                shutil.copy(db_path, backup_db)
                checks["backup_state"] = "PASS"
            else:
                checks["backup_state"] = "PASS (No database file found yet)"
        except Exception as e:
            checks["backup_state"] = f"FAIL ({e})"
            

        # 2a. Back up audit log
        try:
            import shutil
            audit_log_path = os.path.join(DATA_DIR, "audit_log.jsonl")
            if os.path.exists(audit_log_path):
                backup_dir = os.path.join(DATA_DIR, "backups") + "/"
                os.makedirs(backup_dir, exist_ok=True)
                date_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                backup_audit = os.path.join(backup_dir, f"audit_log_{date_str}.jsonl")
                shutil.copy(audit_log_path, backup_audit)
                checks["audit_log_backup"] = "PASS"
            else:
                checks["audit_log_backup"] = "PASS (No audit log file yet)"
        except Exception as e:
            checks["audit_log_backup"] = f"FAIL ({e})"
        # 3. Data feed health (Finnhub)
        try:
            import urllib.request
            token = os.environ.get("FINNHUB_API_KEY", "")
            if token:
                url = f"https://finnhub.io/api/v1/news?category=general&token={token}"
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=5) as response:
                    res_data = json.loads(response.read().decode())
                    if len(res_data) > 0:
                        checks["data_feed_health"] = "PASS"
                    else:
                        checks["data_feed_health"] = "WARNING (Empty news response)"
            else:
                checks["data_feed_health"] = "WARNING (No token, skipped check)"
        except Exception as e:
            checks["data_feed_health"] = f"WARNING ({e})"
            
        # 4. Broker API auth (Alpaca)
        try:
            acct = self.trading.broker.get_account()
            if acct:
                checks["broker_auth"] = "PASS"
            else:
                checks["broker_auth"] = "FAIL (Invalid account response)"
        except Exception as e:
            checks["broker_auth"] = f"FAIL ({e})"
            
        # 5. Place a test order (tiny test and cancel) — only when market is open
        # Fail closed on clock failure: unknown phase with error = unsafe
        market_snapshot = market_hours if market_hours is not None else self.state.market_hours
        mh_phase = market_snapshot.get("phase", "")
        market_is_open = market_snapshot.get("is_open", False)
        if mh_phase == "unknown" and "error" in market_snapshot:
            logger.warning(
                "Market-hours clock FAILED (error=%s) — skipping test order",
                market_snapshot.get("error", "unknown"),
            )
            checks["test_order"] = "SKIP (Clock failure — fail closed)"
        elif not market_is_open:
            logger.info("Market closed — skipping test order")
            checks["test_order"] = "SKIP (Market closed)"
        else:
            try:
                # We place a 1 share limit order far from market on SPY
                if not self.trading.broker.is_simulating:
                    from alpaca.trading.requests import LimitOrderRequest
                    from alpaca.trading.enums import OrderSide, TimeInForce
                    req = LimitOrderRequest(
                        symbol="SPY",
                        qty=1,
                        side=OrderSide.BUY,
                        type="limit",
                        time_in_force=TimeInForce.DAY,
                        limit_price=10.0 # way below market to avoid fill
                    )
                    order = self.trading.broker.trading_client.submit_order(req)
                    if order and order.id:
                        # Cancel immediately
                        self.trading.broker.trading_client.cancel_order_by_id(order.id)
                        checks["test_order"] = "PASS"
                    else:
                        checks["test_order"] = "FAIL (Could not submit test order)"
                else:
                    checks["test_order"] = "PASS (Simulated environment)"
            except Exception as e:
                checks["test_order"] = f"FAIL ({e})"
        # 6. Reconnection check
        try:
            conn = self.patterns.db._connect()
            conn.execute("SELECT 1").fetchone()
            checks["reconnection"] = "PASS"
        except Exception as e:
            checks["reconnection"] = f"FAIL ({e})"
            
        # 7. Historical data backfill check (pre-market: ensure daily_bars has data)
        try:
            bars = self.patterns.db.get_recent_daily_bars("SPY", limit=1)
            if not bars:
                logger.info("Pre-market: daily_bars empty -- triggering one-time backfill.")
                self._run_historical_backfill()
                checks["historical_backfill"] = "PASS (Backfilled on demand)"
            else:
                checks["historical_backfill"] = "PASS (Data already present)"
        except Exception as bfe:
            logger.error("Pre-market backfill FAILED: %s", bfe)
            checks["historical_backfill"] = f"FAIL ({bfe})"
            
        # 8. Health Report finalization
        overall = "PASS"
        if any(v.startswith("FAIL") for v in checks.values()):
            overall = "FAIL"
            # Session-level advisory: set health flag, skip trading, retry next phase.
            # Do NOT touch the operator mode file — this is a transient condition.
            self.state.health_failed_this_session = True
            logger.critical(
                "CRITICAL pre-market health check FAILED — "
                "trading blocked for this session window. "
                "Will retry at next phase transition. "
                "Failures: %s",
                ", ".join(k for k, v in checks.items() if v.startswith("FAIL")),
            )
        else:
            # Health check passed — clear any stale session-health flag
            self.state.health_failed_this_session = False

        report = {
            "timestamp": time.time(),
            "overall_status": overall,
            "checks": checks
        }
        
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, "health_pre_market.json"), "w") as hf:
            json.dump(report, hf, indent=2)
            
        logger.info(f"Pre-Market Health check complete. Overall status={overall}")
        logger.info(
            "health_failed_this_session=%s  killed=%s  mode=%s",
            self.state.health_failed_this_session,
            self.state.killed,
            self.state.mode.value,
        )
        return report

    def get_portfolio_data(self) -> dict:
        """Return portfolio/account information."""
        try:
            account = self.trading.broker.get_account()
            return {
                "buying_power": account.buying_power,
                "equity": account.equity,
                "cash": account.cash,
                "portfolio_value": account.portfolio_value,
                "positions": account.positions,
                "mode": "simulation" if self.trading.broker.is_simulating else "live",
            }
        except Exception as e:
            return {"error": str(e)}

    def get_config(self) -> dict:
        """Return orchestrator configuration."""
        return {
            "poll_interval_s": self.poll_interval,
            "min_conviction_threshold": self.min_conviction,
            "high_conviction_threshold": self.high_conviction,
            "simulation_mode": self.simulate,
            "max_headlines_per_cycle": MAX_HEADLINES,
            "api_port": API_PORT,
            "daily_loss_limit_pct": DAILY_LOSS_LIMIT_PCT,
            "stop_loss_pct": STOP_LOSS_PCT * 100.0,
            "take_profit_pct": TAKE_PROFIT_PCT * 100.0,
            "shadow_signal_threshold": SHADOW_SIGNAL_THRESHOLD,
            "shadow_promotion_min_trades": SHADOW_PROMOTION_MIN_TRADES,
            "shadow_promotion_min_days": SHADOW_PROMOTION_MIN_DAYS,
            "paper_exploration_daily_cap": PAPER_EXPLORATION_DAILY_CAP,
            "paper_exploration_quantity_cap": 1,
            "pattern_execution_min_resolved": PATTERN_EXECUTION_MIN_RESOLVED,
        }

    def get_stats(self) -> dict:
        """Return portfolio-level statistics from the completed trades log."""
        try:
            return self.stats.compute()
        except Exception as e:
            logger.error("get_stats failed: %s", e)
            return {"error": str(e)}


    def get_learning_report(self) -> dict:
        """Return learning summary report for the dashboard."""
        try:
            report = self.patterns.learning_report()
            return report
        except Exception as e:
            return {"error": str(e)}

    def get_milestones(self) -> dict:
        """Return profit milestone summary toward $10K target."""
        try:
            return self.patterns.milestone_summary()
        except Exception as e:
            return {"error": str(e)}

    def get_monitoring_status(self) -> dict:
        """Return monitoring system status."""
        try:
            return self.alerts.status()
        except Exception as e:
            return {"error": str(e)}

    def get_recent_audit(self, limit: int = 20) -> dict:
        """Return the most recent decision-audit records (tail of the JSONL)."""
        try:
            import json
            from monitoring import AUDIT_LOG_PATH
            if not os.path.exists(AUDIT_LOG_PATH):
                return {"records": [], "note": "No audit log yet."}
            with open(AUDIT_LOG_PATH) as f:
                lines = f.readlines()[-limit:]
            records = []
            for ln in lines:
                try:
                    records.append(json.loads(ln))
                except Exception:
                    continue
            return {"count": len(records), "records": records}
        except Exception as e:
            return {"error": str(e)}

    def get_market_regime(self) -> dict:
        """
        Return the current market regime. Recomputes on demand if it has
        never been calculated (e.g. before the first pipeline cycle).
        """
        try:
            if self.state.market_regime.get("regime", "unknown") == "unknown":
                return self._detect_market_regime()
            return self.state.market_regime
        except Exception as e:
            return {"error": str(e), **self.state.market_regime}

    def get_market_hours(self) -> dict:
        """Return live market-hours status (always freshly computed)."""
        try:
            mh = self.clock.status()
            self.state.market_hours = mh
            return mh
        except Exception as e:
            return {"error": str(e), **self.state.market_hours}

    def get_recent_alerts(self) -> dict:
        """Return recent alerts from the local alerts database."""
        try:
            from alert_db import get_recent_alerts as _get_alerts
            return {"alerts": _get_alerts(limit=20)}
        except Exception as e:
            return {"error": str(e)}


    def get_heartbeat(self) -> dict:
        """Return the latest heartbeat data."""
        try:
            import json
            if os.path.exists(self.HEARTBEAT_FILE):
                with open(self.HEARTBEAT_FILE) as f:
                    return json.load(f)
            return {"status": "pending", "message": "No heartbeat recorded yet."}
        except Exception as e:
            return {"error": str(e)}


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------
_orchestrator: Optional[Orchestrator] = None


def _signal_handler(sig, frame):
    logger.info("Received signal %s. Shutting down...", sig)
    if _orchestrator:
        _orchestrator.stop()
    release_process_lock()
    sys.exit(0)


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Parse arguments
    args = set(sys.argv[1:])
    simulate = "--simulate" in args or "-s" in args or "--live" not in args
    api_only = "--api-only" in args

    # ---- Refuse to silently ignore a flag ----
    # The systemd unit passed `--autonomous` and nothing parsed it, so the
    # unit file promised autonomous operation while the bot started MANUAL.
    # Mode is owned by the operator mode file (docs/MODE_PRECEDENCE.md): "No
    # env vars, no CLI flags." That is a deliberate contract -- but a control
    # that is ignored in silence is worse than one that is refused, so say so.
    _KNOWN_FLAGS = {"--simulate", "-s", "--live", "--api-only",
                    "--extended-hours"}
    _unknown = {a for a in args if a.startswith("-")} - _KNOWN_FLAGS
    if _unknown:
        _log = logging.getLogger("educator")
        _log.warning(
            "Ignoring unrecognised flag(s): %s. Mode is not settable from the "
            "command line -- it comes from %s (see docs/MODE_PRECEDENCE.md). "
            "Use POST /api/mode to change it.",
            ", ".join(sorted(_unknown)),
            os.path.join(DATA_DIR, "orchestrator_mode.txt"))

    # ---- Mandatory auth token, checked before anything binds a socket ----
    # This check used to live inside Orchestrator.start(), which --api-only
    # never calls, so `main.py --api-only` bound an unauthenticated control
    # API. The requirement belongs to the process, not to one startup path.
    if not os.environ.get("API_AUTH_TOKEN", ""):
        logging.getLogger("educator").critical(
            "API_AUTH_TOKEN is required -- the control API can change mode "
            "and place trades. Set a strong token and restart.")
        sys.exit(1)

    # Acquire the environment-scoped singleton before constructing the broker
    # or running recovery. A duplicate must do no work and must not compete
    # for the control port, broker session, ledger, or position state.
    try:
        acquire_process_lock(DATA_DIR)
    except RuntimeError as exc:
        logging.getLogger("educator").critical("Startup refused: %s", exc)
        sys.exit(1)

    extended_hours = (
        "--extended-hours" in args
        or os.environ.get("ALLOW_EXTENDED_HOURS", "").strip().lower()
        in ("1", "true", "yes", "on")
    )

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Create orchestrator
    orch = Orchestrator(
        simulate=simulate,
        allow_extended_hours=extended_hours,
    )
    _orchestrator = orch

    print(f"""
{'='*60}
  Educated Trades — Backend Orchestrator
{'='*60}
  Mode:       {orch.state.mode.value.upper() if hasattr(orch, 'state') else 'MANUAL'}
  Simulation: {'YES' if simulate else 'LIVE'}
  API:        http://{API_BIND}:{API_PORT}
  Poll:       Every {DEFAULT_POLL_INTERVAL_S}s
{'='*60}

  API Endpoints:
    GET  /api/status          System status
    GET  /api/sentiment/latest Latest sentiment analysis
    GET  /api/patterns/top    Pattern memory summary
    GET  /api/trades/recent   Recent trade history
    GET  /api/portfolio       Portfolio / account info
    GET  /api/evaluate        On-demand evaluation
    POST /api/mode            Set mode: manual | autonomous | stopped
    POST /api/evaluate        Evaluate symbol (body: {{"symbol":"SPY"}})
    POST /api/execute         Execute trade (body: {{"symbol":"SPY","conviction":0.65}})

  Controls:
    [Ctrl+C]  Graceful shutdown
{'='*60}
""")

    if api_only:
        logger.info("Starting in API-only mode (no pipeline)")
        orch._api_server = create_api_server(API_BIND, API_PORT, orch)
        orch._api_thread = threading.Thread(
            target=orch._api_server.serve_forever,
            daemon=True,
        )
        orch._api_thread.start()
        orch.state.running = True
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        orch.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            orch.stop()
            release_process_lock()
            print("\nOrchestrator stopped. Goodbye!")
