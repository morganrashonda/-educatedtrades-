"""
Real-time Trading Execution Core for Educated Trades.

Interfaces with the Alpaca API for US stock order execution and
provides a placeholder for Pocket Option (Binary Options) integration.

Receives signals from the Sentiment Engine and Pattern-Recognition Engine,
evaluates combined conviction, and executes trades accordingly.

Credentials (via environment variables):
    APCA_API_KEY_ID      — Alpaca API key ID
    APCA_API_SECRET_KEY  — Alpaca API secret key
    APCA_BASE_URL        — Alpaca base URL (default: paper trading endpoint)

Usage:
    from trading import TradingEngine, TradeSignal

    engine = TradingEngine()
    signal = TradeSignal(
        symbol="SPY", action="buy", conviction=0.65,
        source="sentiment+pattern", reason="Bullish sentiment + pattern match"
    )
    result = engine.execute(signal)
    print(result)
"""

import contextlib
import logging
import os
import time
from dataclasses import asdict as _dataclass_asdict, dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

class BrokerPositionError(RuntimeError):
    """Broker position query failed; an empty list is not a valid answer."""


# Lazy import for position state persistence (avoids circular deps at module level)
_position_state_mgr = None


def _get_position_state():
    """Lazy singleton accessor for PositionStateManager."""
    global _position_state_mgr
    if _position_state_mgr is None:
        from position_state import PositionStateManager, build_position_record
        _position_state_mgr = PositionStateManager()
    return _position_state_mgr


def _persist_open_position(
    symbol: str, qty: int, side: str, entry_price: float,
    entry_time: Optional[float] = None,
    position_id=None, broker_order_id=None,
    stop_loss: Optional[float] = None, take_profit: Optional[float] = None,
    strategy: Optional[str] = None, regime: Optional[str] = None,
    conviction: Optional[float] = None,
) -> None:
    """Best-effort persist a newly opened position to the state file."""
    try:
        from position_state import build_position_record
        record = build_position_record(
            symbol=symbol, qty=qty, side=side,
            entry_price=entry_price, entry_time=entry_time,
            position_id=position_id, broker_order_id=broker_order_id,
            stop_loss=stop_loss, take_profit=take_profit,
            strategy=strategy, regime=regime, conviction=conviction,
        )
        _get_position_state().add_position(record)
    except Exception as e:
        logger.warning("Failed to persist open position for %s: %s", symbol, e)


def _remove_persisted_position(symbol: str) -> None:
    """Best-effort remove a closed position from the state file."""
    try:
        _get_position_state().remove_position(symbol)
    except Exception as e:
        logger.warning("Failed to remove persisted position for %s: %s", symbol, e)


def _audit(event_type: str, data: dict) -> None:
    """
    Best-effort append to the decision audit log. Order-lifecycle events
    (submission, fill, rejection) are recorded here so every trade can be
    reconstructed. Never raises — auditing must not break execution.
    """
    try:
        from monitoring import DecisionAuditLogger
        DecisionAuditLogger.instance().log(event_type, data)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default Alpaca paper trading URL
DEFAULT_ALPACA_URL = "https://paper-api.alpaca.markets"
LIVE_ALPACA_URL = "https://api.alpaca.markets"


def detect_environment(api_key: str = "", base_url: str = "") -> str:
    """Return "live" or "paper" from the credentials in use.

    Alpaca issues different keys for the two, and the endpoints differ. The
    environment must be DERIVED, never assumed: `paper=True` used to be
    hardcoded, so live credentials would have been pointed at the paper
    endpoint. Ambiguity resolves to "paper" -- guessing wrong toward paper
    costs nothing, guessing wrong toward live spends real money.
    """
    url = (base_url or os.environ.get("APCA_BASE_URL", "") or "").lower()
    if url:
        if "paper" in url:
            return "paper"
        if "api.alpaca.markets" in url:
            return "live"
    key = (api_key or os.environ.get("APCA_API_KEY_ID", "") or "").strip()
    # Alpaca paper keys are conventionally PK-prefixed, live keys AK-.
    if key.upper().startswith("PK"):
        return "paper"
    if key.upper().startswith("AK"):
        return "live"
    return "paper"


def resolve_data_dir() -> str:
    """The data directory, derived from the credentials. The single answer.

    It lives here, next to detect_environment and below every other module,
    because the alternative was each entry point deriving its own: main.py,
    the external health check, the decision journal. Three copies is three
    chances to disagree, and they did -- the journal's fallback was "." and
    anything that used a TradingEngine without importing main first wrote the
    record of WHY a trade was taken into whatever directory it happened to be
    started from.

    main.py exports the result to the environment at startup, so in the normal
    case every module reads the same string rather than recomputing it. This
    is the answer for everything else: standalone tools, backtests, scripts.
    """
    if data_dir_is_explicit():
        return os.environ["DATA_DIR"]
    return os.path.join(os.environ.get("DATA_ROOT", "/home/team/shared/data"),
                        detect_environment())


#: Name of the variable recording the DATA_DIR the orchestrator exported.
DATA_DIR_AUTOSET = "DATA_DIR_AUTOSET"


def data_dir_is_explicit() -> bool:
    """True when an operator set DATA_DIR, not when we exported it ourselves.

    main.py writes the derived path into the environment so that every module
    and subprocess agrees. Without this distinction the derivation reads back
    its own answer and stops being idempotent -- it would never notice the
    credentials had changed, which is precisely the moment it matters.
    """
    value = os.environ.get("DATA_DIR")
    return bool(value) and value != os.environ.get(DATA_DIR_AUTOSET)

# Min/max position sizes (as fraction of portfolio)
DEFAULT_MAX_POSITION_SIZE = 0.15       # max 15% per position
DEFAULT_RISK_PER_TRADE = 0.005         # risk 0.5% of capital per trade
# Portfolio-level ceilings. Per-position caps do NOT bound correlated
# exposure: SPY/QQQ/IWM move together, so three "independent" 15% positions
# behave like one 45% directional bet. These limit the whole book.
DEFAULT_MAX_TOTAL_EXPOSURE = float(os.environ.get("MAX_TOTAL_EXPOSURE", "0.30"))
DEFAULT_MAX_CONCURRENT_POSITIONS = int(os.environ.get("MAX_CONCURRENT_POSITIONS", "3"))

# Risk assumed for a position that will be carried through a session close.
# An intraday stop cannot be enforced across a gap: the stop is evaluated on
# bar closes, and a gapped stop order fills at the open, not at the stop
# price. SPY gaps 1-2% overnight routinely, which is several times a 30-minute
# stop. So an overnight position must be SIZED against the risk that can
# actually materialise, not against the stop we hope to use.
OVERNIGHT_RISK_PCT = float(os.environ.get("OVERNIGHT_RISK_PCT", "0.025"))
#: Minimum gap between entries on the same symbol. Persisted via the ledger,
#: so it survives a restart.
ENTRY_COOLDOWN_S = int(os.environ.get("ENTRY_COOLDOWN_S", "300"))

# Default position-safety thresholds (fraction of entry price).
# Baseline per owner directive (2026-06-30): a -2.5% stop gives trades more
# room to breathe, and a +3.0% target locks in profit (~1.2:1 reward/risk).
DEFAULT_STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PCT", "0.025"))
DEFAULT_TAKE_PROFIT_PCT = float(os.environ.get("TAKE_PROFIT_PCT", "0.03"))

# Execution lateness target (KPI: <2 seconds from signal to execution)
TARGET_EXECUTION_LATENCY_S = 2.0


# ---------------------------------------------------------------------------
# Enums & Data Structures
# ---------------------------------------------------------------------------
class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"

class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"

class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    ERROR = "error"


@dataclass
class TradeSignal:
    """
    A trade signal generated by either the Sentiment Engine or
    Pattern- Recognition Engine (or a combination of both).

    Can be instantiated directly or constructed via the helper
    `TradeSignal.from_engines()`.
    """
    symbol: str
    action: str                    # strong_buy | buy | neutral | sell | strong_sell
    conviction: float              # -1.0 to +1.0
    source: str                    # "sentiment" | "pattern" | "sentiment+pattern"
    reason: str                    # Human-readable reason for the signal
    quantity: Optional[int] = None
    order_type: str = "market"     # market | limit
    limit_price: Optional[float] = None
    # Position-safety thresholds (fraction of entry price, e.g. 0.015 = 1.5%).
    # When set, the broker attaches a protective bracket (stop + target) so the
    # position is never left unmanaged. Defaults to the conservative module
    # constants so every signal carries protection unless explicitly cleared.
    stop_loss_pct: Optional[float] = DEFAULT_STOP_LOSS_PCT
    take_profit_pct: Optional[float] = DEFAULT_TAKE_PROFIT_PCT
    timestamp: float = field(default_factory=time.time)

    @property
    def side(self) -> Optional[OrderSide]:
        """Convert action to an order side."""
        if self.action in ("strong_buy", "buy"):
            return OrderSide.BUY
        elif self.action in ("strong_sell", "sell"):
            return OrderSide.SELL
        return None

    @property
    def is_actionable(self) -> bool:
        """Returns True if this signal warrants an order."""
        return self.side is not None and abs(self.conviction) >= 0.3

    @classmethod
    def from_engines(
        cls,
        symbol: str,
        sentiment_conviction: float,
        pattern_signal: Optional['EvaluationSignal'] = None,
        sentiment_reason: str = "",
    ) -> "TradeSignal":
        """
        Combine outputs from the Sentiment and Pattern engines into
        a single trade signal.

        Blending logic:
          - If both agree (both bullish or both bearish): boosted conviction
          - If they disagree: weighted by pattern confidence
          - Default: use sentiment, modulated by pattern if available
        """
        import sys
        sys.path.insert(0, "/home/team/shared/backend")
        try:
            from patterns import EvaluationSignal as ES
        except ImportError:
            ES = None

        if pattern_signal is not None:
            pattern_conv = pattern_signal.conviction
            pattern_action = pattern_signal.action
            source = "sentiment+pattern"
        else:
            pattern_conv = 0.0
            pattern_action = "neutral"
            source = "sentiment"

        # Blend convictions
        if abs(sentiment_conviction) >= abs(pattern_conv):
            # Sentiment is the primary driver
            blended = 0.55 * sentiment_conviction + 0.45 * pattern_conv
        else:
            # Pattern is the primary driver
            blended = 0.35 * sentiment_conviction + 0.65 * pattern_conv

        blended = max(-1.0, min(1.0, round(blended, 4)))

        # Determine action from blended conviction
        if blended >= 0.7:
            action = "strong_buy"
        elif blended >= 0.3:
            action = "buy"
        elif blended <= -0.7:
            action = "strong_sell"
        elif blended <= -0.3:
            action = "sell"
        else:
            action = "neutral"

        # Build reason
        parts = [
            f"Sentiment conviction: {sentiment_conviction:+.3f}",
            f"Pattern conviction: {pattern_conv:+.3f}",
        ]
        if pattern_signal is not None and hasattr(pattern_signal, 'reason'):
            parts.append(f"Pattern: {pattern_signal.reason[:100]}")
        if sentiment_reason:
            parts.append(sentiment_reason)

        return cls(
            symbol=symbol,
            action=action,
            conviction=blended,
            source=source,
            reason=" | ".join(parts),
        )


def bounded_signal_quantity(calculated: int, requested: Optional[int]) -> int:
    """Apply a smaller explicit quantity without bypassing risk sizing.

    The normal risk engine remains the ceiling. This is used by the bounded
    one-share paper exploration tier: it can reduce an order, never enlarge it.
    """
    calculated = int(calculated)
    if requested is None:
        return calculated
    if isinstance(requested, bool) or not isinstance(requested, int) or requested <= 0:
        raise ValueError("signal quantity must be a positive integer")
    return min(calculated, requested)


@dataclass
class ExecutionResult:
    """Result of a trade execution attempt."""
    success: bool
    symbol: str
    side: Optional[OrderSide]
    quantity: int
    filled_price: Optional[float]
    filled_qty: int
    order_id: Optional[str]
    status: OrderStatus
    latency_ms: float
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    error: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def dict(self) -> dict:
        return {
            "success": self.success,
            "symbol": self.symbol,
            "side": self.side.value if self.side else None,
            "quantity": self.quantity,
            "filled_price": self.filled_price,
            "filled_qty": self.filled_qty,
            "order_id": self.order_id,
            "status": self.status.value,
            "latency_ms": round(self.latency_ms, 1),
            "error": self.error,
        }


@dataclass
class AccountInfo:
    """Snapshot of account information."""
    buying_power: float = 0.0
    cash: float = 0.0
    portfolio_value: float = 0.0
    long_market_value: float = 0.0
    short_market_value: float = 0.0
    equity: float = 0.0
    is_paper: bool = True
    positions: List[Dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pocket Option Placeholder
# ---------------------------------------------------------------------------
class PocketOptionBroker:
    """
    Placeholder for Pocket Option (Binary Options) integration.

    This provides the interface structure so the rest of the system can
    signal binary-options trades. Actual API integration requires
    Pocket Option's proprietary API keys and library.
    """

    def __init__(self):
        self._connected = False
        self._demo_mode = True
        logger.debug("PocketOptionBroker placeholder initialised (demo mode)")

    def connect(self, api_key: str = "", demo: bool = True) -> bool:
        """
        Connect to Pocket Option (placeholder — always logs intent).
        """
        self._demo_mode = demo
        self._connected = True
        logger.debug(
            "PocketOptionBroker.connect() called — "
            "demo=%s. Real API TBD.", demo
        )
        return True

    def place_binary_option(
        self,
        symbol: str,
        direction: str,       # "call" | "put"
        amount: float,
        expiry_minutes: int = 5,
    ) -> dict:
        """
        Place a binary option trade (placeholder).

        Args:
            symbol: Trading symbol (e.g., "EURUSD")
            direction: "call" (up) or "put" (down)
            amount: Dollar amount to risk
            expiry_minutes: Option expiry in minutes

        Returns:
            dict with status and placeholder trade id.
        """
        if not self._connected:
            return {"status": "error", "error": "Not connected"}

        logger.debug(
            "PocketOption: %s %s $%.2f %dmin (PLACEHOLDER — not executed)",
            direction.upper(), symbol, amount, expiry_minutes,
        )
        return {
            "status": "demo_placed",
            "symbol": symbol,
            "direction": direction,
            "amount": amount,
            "expiry_minutes": expiry_minutes,
            "trade_id": f"demo-{int(time.time())}",
            "note": "Pocket Option integration — placeholder only",
        }

    def get_balance(self) -> dict:
        """Get demo account balance (placeholder)."""
        return {
            "balance": 10000.0,
            "currency": "USD",
            "mode": "demo" if self._demo_mode else "real",
        }


# ---------------------------------------------------------------------------
# Alpaca Broker
# ---------------------------------------------------------------------------
class AlpacaBroker:
    """
    Wraps the Alpaca trading API for US stock order execution.

    Falls back to simulated execution if credentials are not available.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        simulate: bool = False,
    ):
        """
        Args:
            api_key: Alpaca API key ID (defaults to APCA_API_KEY_ID env var)
            api_secret: Alpaca secret key (defaults to APCA_API_SECRET_KEY env var)
            base_url: Alpaca API URL (defaults to APCA_BASE_URL or paper endpoint)
            simulate: Force simulation mode even if credentials exist
        """
        self._api_key = api_key or os.environ.get("APCA_API_KEY_ID", "")
        self._api_secret = api_secret or os.environ.get("APCA_API_SECRET_KEY", "")
        self._base_url = (
            base_url
            or os.environ.get("APCA_BASE_URL", "")
            or DEFAULT_ALPACA_URL
        )

        self._simulate = simulate
        if self._simulate and (self._api_key or self._api_secret):
            raise ValueError("Simulation mode cannot run with paper/live Alpaca credentials")
        self.environment = detect_environment(self._api_key, self._base_url)
        self.is_live = self.environment == "live"
        self._client = None
        self._connected = False
        self._initialization_error: Optional[BaseException] = None
        self._initialise()

    def _initialise(self) -> None:
        """Attempt to initialise the Alpaca client."""
        if self._simulate:
            logger.debug("AlpacaBroker: simulation mode (forced)")
            return

        if not self._api_key or not self._api_secret:
            logger.warning(
                "Alpaca API credentials not found. "
                "Set APCA_API_KEY_ID and APCA_API_SECRET_KEY env vars, "
                "or pass simulate=True for simulation mode."
            )
            self._simulate = True
            return

        try:
            from alpaca.trading.client import TradingClient
            # Derived, not assumed. Hardcoding paper=True meant live keys
            # were still routed at the paper endpoint.
            if self.is_live:
                logger.critical(
                    "AlpacaBroker connecting to LIVE trading — orders will use "
                    "real money.")
            self._client = TradingClient(
                api_key=self._api_key,
                secret_key=self._api_secret,
                paper=not self.is_live,
            )
            # Quick connectivity test
            account = self._client.get_account()
            self._connected = True
            logger.info(
                "AlpacaBroker connected (environment=%s, buying_power=%.2f)",
                self.environment, float(account.buying_power),
            )
        except Exception as e:
            logger.error("AlpacaBroker initialisation failed: %s", e)
            self._initialization_error = e
            self._connected = False

    def reconnect(self) -> bool:
        """Retry live broker initialization without entering simulation mode."""
        if self._simulate:
            return False
        self._client = None
        self._connected = False
        self._initialization_error = None
        self._initialise()
        return self._connected

    @property
    def is_simulating(self) -> bool:
        return self._simulate

    # ------------------------------------------------------------------
    # Account Info
    # ------------------------------------------------------------------
    def get_account(self) -> AccountInfo:
        """Fetch current account information."""
        info = AccountInfo()

        if self._client and self._connected:
            try:
                account = self._client.get_account()
                info.buying_power = float(account.buying_power)
                info.cash = float(account.cash)
                info.portfolio_value = float(account.portfolio_value)
                info.long_market_value = float(account.long_market_value)
                info.short_market_value = float(account.short_market_value)
                info.equity = float(account.equity)
                info.is_paper = not self.is_live

                positions = self._client.get_all_positions()
                info.positions = [
                    {
                        "symbol": p.symbol,
                        "qty": float(p.qty),
                        "market_value": float(p.market_value),
                        "cost_basis": float(p.cost_basis),
                        "unrealized_pl": float(p.unrealized_pl),
                        "unrealized_plpc": float(p.unrealized_plpc),
                    }
                    for p in positions
                ]
            except Exception as e:
                logger.error("Failed to fetch account info: %s", e)
        else:
            # Simulation mode — return a mock account
            info.buying_power = 100000.0
            info.cash = 50000.0
            info.portfolio_value = 100000.0
            info.equity = 100000.0

        return info

    # ------------------------------------------------------------------
    # Order Execution
    # ------------------------------------------------------------------
    def execute_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Optional[float] = None,
        time_in_force: str = "day",
        stop_loss_pct: Optional[float] = None,
        take_profit_pct: Optional[float] = None,
        reference_price: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> ExecutionResult:
        """
        Execute an order via Alpaca (or simulation).

        If ``stop_loss_pct`` and/or ``take_profit_pct`` are supplied (and the
        order is a BUY market order), a protective bracket is attached so the
        position is automatically closed by the broker at the stop or target —
        the position is never left unmanaged even if the orchestrator stalls.

        Args:
            stop_loss_pct: Stop distance as a fraction of entry (e.g. 0.015).
            take_profit_pct: Target distance as a fraction of entry.
            reference_price: Price used to compute absolute stop/limit levels.
                Falls back to a live/reference quote when not provided.

        Returns:
            ExecutionResult with fill details and latency measurement.
        """
        start_time = time.time()

        if quantity <= 0:
            _audit("order_result", {
                "symbol": symbol, "side": getattr(side, "value", None),
                "quantity": quantity, "success": False,
                "status": "rejected", "error": "Quantity must be > 0",
            })
            return ExecutionResult(
                success=False, symbol=symbol, side=side,
                quantity=quantity, filled_price=None, filled_qty=0,
                order_id=None, status=OrderStatus.REJECTED,
                latency_ms=0, error="Quantity must be > 0",
            )

        # --- Audit: order submission (incl. bracket SL/TP parameters) ---
        _audit("order_submit", {
            "symbol": symbol,
            "side": getattr(side, "value", None),
            "quantity": quantity,
            "order_type": getattr(order_type, "value", str(order_type)),
            "limit_price": limit_price,
            "time_in_force": time_in_force,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "reference_price": reference_price,
            "bracket": bool(stop_loss_pct or take_profit_pct),
            "mode": "simulation" if self._simulate else "live",
        })

        # Dispatch on INTENT, never on connectivity.
        #
        # This read `if self._client and self._connected: live else: simulated`.
        # Simulation was therefore a FALLBACK: a live broker whose connection
        # dropped -- `_simulate` still False, `_connected` now False -- fell
        # through and produced a FABRICATED FILL, while the audit record above
        # said `"mode": "live"`. The system would hold a position that the
        # broker never received, with P&L, learning data and slippage all
        # computed from a price nobody traded at.
        #
        # Two real states reach that: live initialisation failing, and
        # reconnect() failing. A DNS outage during an order is enough.
        #
        # Simulation is a mode you choose, not a thing that happens to you.
        # Live intent with no broker must REFUSE, exactly like every other
        # unresolvable state in this system.
        if self._simulate:
            result = self._execute_simulated(
                symbol, side, quantity, order_type, limit_price,
            )
        elif self._client and self._connected:
            result = self._execute_live(
                symbol, side, quantity, order_type, limit_price, time_in_force,
                stop_loss_pct, take_profit_pct, reference_price, client_order_id,
            )
        else:
            detail = ("broker not connected"
                      if self._client else "broker client not initialised")
            if self._initialization_error:
                detail += " (%s)" % self._initialization_error
            logger.error(
                "REFUSING %s %s %s: live intent but %s. No order placed, and "
                "no simulated fill fabricated.",
                getattr(side, "value", side), quantity, symbol, detail)
            result = ExecutionResult(
                success=False, symbol=symbol, side=side, quantity=0,
                filled_price=None, filled_qty=0, order_id=None,
                status=OrderStatus.REJECTED, latency_ms=0,
                error="Broker unavailable: %s" % detail,
                details={"refused_no_broker": True,
                         "environment": self.environment},
            )

        result.latency_ms = round((time.time() - start_time) * 1000, 1)

        # --- Audit: order result (fill or rejection + full broker response) ---
        _audit("order_result", {
            "symbol": symbol,
            "side": getattr(side, "value", None),
            "quantity": quantity,
            "success": result.success,
            "status": result.status.value,
            "filled_price": result.filled_price,
            "filled_qty": result.filled_qty,
            "order_id": result.order_id,
            "latency_ms": result.latency_ms,
            "error": result.error,
            "details": result.details,
        })

        if result.success:
            logger.debug(
                "Order executed: %s %d %s @ %.2f (latency=%.0fms)",
                side.value.upper(), quantity, symbol,
                result.filled_price or 0, result.latency_ms,
            )
        else:
            logger.warning(
                "Order failed: %s %d %s — %s",
                side.value.upper(), quantity, symbol,
                result.error or "unknown",
            )

        return result

    def _execute_live(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType,
        limit_price: Optional[float],
        time_in_force: str,
        stop_loss_pct: Optional[float] = None,
        take_profit_pct: Optional[float] = None,
        reference_price: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> ExecutionResult:
        """Execute via live Alpaca API."""
        try:
            from alpaca.trading.requests import (
                MarketOrderRequest, LimitOrderRequest,
                TakeProfitRequest, StopLossRequest,
            )
            from alpaca.trading.enums import (
                OrderSide as AlpacaSide, TimeInForce, OrderClass,
            )

            # ---- Build protective bracket (if requested) ----------------
            # A bracket order submits the entry plus a one-cancels-other pair
            # of protective exits (take-profit limit + stop-loss stop) in a
            # single atomic request, so the broker enforces the exits even if
            # our process dies. Only long market entries are bracketed here.
            take_profit_req = None
            stop_loss_req = None
            bracket_note = "none"
            if (
                order_type == OrderType.MARKET
                and (side == OrderSide.BUY or side == OrderSide.SELL)
                and (stop_loss_pct or take_profit_pct)
            ):
                ref = reference_price
                if ref is None or ref <= 0:
                    try:
                        ref = TradingEngine._get_reference_price(symbol)
                    except Exception:
                        ref = None
                if ref and ref > 0:
                    is_short = side == OrderSide.SELL
                    if take_profit_pct:
                        # TP is above entry for buys, below entry for shorts
                        tp_mult = 1 + take_profit_pct if not is_short else 1 - take_profit_pct
                        tp_price = round(ref * tp_mult, 2)
                        take_profit_req = TakeProfitRequest(limit_price=tp_price)
                    if stop_loss_pct:
                        # SL is below entry for buys, above entry for shorts
                        sl_mult = 1 - stop_loss_pct if not is_short else 1 + stop_loss_pct
                        sl_price = round(ref * sl_mult, 2)
                        stop_loss_req = StopLossRequest(stop_price=sl_price)
                    bracket_note = (
                        f"ref={ref} sl_pct={stop_loss_pct} tp_pct={take_profit_pct}"
                    )
                else:
                    logger.warning(
                        "Bracket requested for %s but no reference price "
                        "available — submitting plain market order.", symbol,
                    )

            if order_type == OrderType.MARKET:
                use_bracket = take_profit_req is not None or stop_loss_req is not None
                order_data = MarketOrderRequest(
                    symbol=symbol,
                    qty=quantity,
                    side=AlpacaSide.BUY if side == OrderSide.BUY else AlpacaSide.SELL,
                    # Bracket legs require GTC time-in-force for equities.
                    time_in_force=TimeInForce.GTC if use_bracket else TimeInForce.DAY,
                    order_class=OrderClass.BRACKET if use_bracket else OrderClass.SIMPLE,
                    take_profit=take_profit_req,
                    stop_loss=stop_loss_req,
                )
                if client_order_id:
                    # Broker-side idempotency: Alpaca rejects a duplicate
                    # client_order_id, so a retry can never double-submit.
                    order_data.client_order_id = client_order_id
            else:
                if limit_price is None:
                    return ExecutionResult(
                        success=False, symbol=symbol, side=side,
                        quantity=quantity, filled_price=None, filled_qty=0,
                        order_id=None, status=OrderStatus.REJECTED,
                        latency_ms=0, error="Limit price required for limit orders",
                    )
                order_data = LimitOrderRequest(
                    symbol=symbol,
                    qty=quantity,
                    side=AlpacaSide.BUY if side == OrderSide.BUY else AlpacaSide.SELL,
                    limit_price=round(limit_price, 2),
                    time_in_force=TimeInForce.DAY,
                )
                if client_order_id:
                    order_data.client_order_id = client_order_id

            order = self._client.submit_order(order_data)

            # Fill state is derived ONLY from broker-reported quantities.
            # Never infer a fill from the requested quantity, and never treat
            # an acknowledged-but-unfilled order as a completed trade.
            alpaca_status = str(
                getattr(getattr(order, "status", ""), "value",
                        getattr(order, "status", "")) or "").strip().lower()

            try:
                filled_qty = int(getattr(order, "filled_qty", 0) or 0)
            except (TypeError, ValueError):
                filled_qty = 0
            filled_qty = max(0, filled_qty)

            raw_price = getattr(order, "filled_avg_price", None)
            try:
                filled_price = float(raw_price) if raw_price is not None else None
            except (TypeError, ValueError):
                filled_price = None
            if filled_price is not None and not filled_price > 0:
                filled_price = None

            # Alpaca spells it "canceled"; accept both spellings.
            if filled_qty >= quantity and filled_qty > 0 and filled_price is not None:
                status = OrderStatus.FILLED
            elif filled_qty > 0:
                status = OrderStatus.PARTIALLY_FILLED
            elif alpaca_status in ("canceled", "cancelled", "expired"):
                status = OrderStatus.CANCELLED
            elif alpaca_status == "rejected":
                status = OrderStatus.REJECTED
            else:
                status = OrderStatus.PENDING

            return ExecutionResult(
                success=filled_qty > 0,
                symbol=symbol, side=side, quantity=quantity,
                filled_price=filled_price, filled_qty=filled_qty,
                order_id=order.id, status=status,
                latency_ms=0,
                details={
                    "alpaca_status": order.status,
                    "order_id": order.id,
                    "bracket": bracket_note,
                    "order_class": getattr(order, "order_class", None),
                },
            )

        except Exception as e:
            return ExecutionResult(
                success=False, symbol=symbol, side=side,
                quantity=quantity, filled_price=None, filled_qty=0,
                order_id=None, status=OrderStatus.ERROR, latency_ms=0,
                error=f"Alpaca API error: {e}",
            )

    def _execute_simulated(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType,
        limit_price: Optional[float],
    ) -> ExecutionResult:
        """Simulate order execution with realistic fill assumptions."""
        import random

        # Mock price — in production, this would come from a real quote
        mock_price = 450.0 + (hash(symbol) % 2000) / 100.0
        if limit_price and order_type == OrderType.LIMIT:
            fill_price = limit_price
        else:
            # Simulate small slippage
            slippage = random.uniform(-0.15, 0.15)
            fill_price = round(mock_price * (1 + slippage), 2)

        return ExecutionResult(
            success=True, symbol=symbol, side=side,
            quantity=quantity, filled_price=fill_price,
            filled_qty=quantity,
            order_id=f"sim-{int(time.time()*1000)}",
            status=OrderStatus.FILLED,
            latency_ms=0,
            details={"simulated": True, "slippage_pct": round(slippage, 4)},
        )

    # ------------------------------------------------------------------
    # Position Management
    # ------------------------------------------------------------------
    def close_position(self, symbol: str) -> ExecutionResult:
        """Close an open position. `success` means CONFIRMED FLAT, nothing less.

        Two defects lived here, both of the same family: the function claimed
        more than it knew.

        1. `success=True` was returned whether or not the broker confirmed the
           position was gone -- only `status` distinguished FILLED from
           PENDING, and every caller keys on `.success`. An unconfirmed close
           was therefore recorded as a completed trade: phantom P&L, tracking
           removed, and a live position left with nothing watching it.

        2. Dispatch keyed on `_connected`, so a live broker that lost its
           connection fell through to the simulated branch and returned
           `success=True, FILLED` for a close that was never submitted -- then
           deleted the persisted position. The bot would believe it was flat
           while holding real exposure.

        Now: simulation runs only when simulation is the chosen mode; live
        intent without a broker refuses; and success requires the broker to
        confirm flatness. An unconfirmed submission returns success=False with
        the exit still recorded, so reconciliation resolves it rather than the
        caller assuming it is done.
        """
        if self._simulate:
            res = ExecutionResult(
                success=True, symbol=symbol, side=None,
                quantity=0, filled_price=None, filled_qty=0,
                order_id="sim-close-%d" % int(time.time()),
                status=OrderStatus.FILLED, latency_ms=0,
                details={"simulated": True},
            )
            _remove_persisted_position(symbol)
            return res

        if not (self._client and self._connected):
            detail = ("broker not connected"
                      if self._client else "broker client not initialised")
            logger.error(
                "REFUSING to close %s: live intent but %s. The position may "
                "still be OPEN at the broker -- not marking it closed.",
                symbol, detail)
            return ExecutionResult(
                success=False, symbol=symbol, side=None, quantity=0,
                filled_price=None, filled_qty=0, order_id=None,
                status=OrderStatus.ERROR, latency_ms=0,
                error="Broker unavailable: %s" % detail,
                details={"refused_no_broker": True, "confirmed_flat": False},
            )

        # The simulated branch that used to sit at the end of this function is
        # now handled at the top, gated on `_simulate` rather than on
        # connectivity. Reaching it via `else` meant a disconnected LIVE
        # broker reported a successful close it never submitted.
        try:
            closed = self._client.close_position(symbol)
            # close_position only SUBMITS a liquidating order. Treating it as
            # filled -- and dropping local state -- would strand a live
            # position if it does not fill. Confirm flatness first.
            confirmed_flat = False
            try:
                confirmed_flat = self.get_position_strict(symbol) is None
            except Exception as probe_error:
                logger.warning(
                    "Could not confirm %s is flat after close: %s",
                    symbol, probe_error,
                )
            if not confirmed_flat:
                logger.warning(
                    "Close for %s SUBMITTED but not confirmed flat. Reporting "
                    "failure so the caller does not record a completed trade; "
                    "reconciliation will resolve it.", symbol)
            res = ExecutionResult(
                # success == the broker confirmed the position is gone.
                # Anything less is an unresolved exit, not a closed trade.
                success=bool(confirmed_flat), symbol=symbol, side=None,
                quantity=0, filled_price=None, filled_qty=0,
                order_id=closed.id if hasattr(closed, 'id') else None,
                status=OrderStatus.FILLED if confirmed_flat else OrderStatus.PENDING,
                latency_ms=0,
                details={"confirmed_flat": confirmed_flat,
                         "close_submitted": True},
            )
            # Only forget the position once the broker confirms it is gone.
            if confirmed_flat:
                _remove_persisted_position(symbol)
        except Exception as e:
            res = ExecutionResult(
                success=False, symbol=symbol, side=None,
                quantity=0, filled_price=None, filled_qty=0,
                order_id=None, status=OrderStatus.ERROR, latency_ms=0,
                error=str(e),
            )

        _audit("order_close", {
            "symbol": symbol, "success": res.success,
            "status": res.status.value, "order_id": res.order_id,
            "error": res.error,
            "mode": "simulation" if self._simulate else "live",
        })
        return res

    @staticmethod
    def _is_no_position_error(exc: BaseException) -> bool:
        """True when the broker is saying 'no such position' (i.e. flat).

        Alpaca raises rather than returning None when a symbol has no open
        position, so this is the only way to distinguish genuinely flat from
        an outage. Anything unrecognised is treated as an outage.
        """
        text = str(exc).lower()
        if getattr(exc, "status_code", None) == 404:
            return True
        return ("position does not exist" in text
                or "position not found" in text
                or "404" in text)

    def get_position_strict(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Return the position, or None when confirmed flat.

        Raises when the broker could not be reached. Callers that must not
        confuse 'flat' with 'unknown' (position reconciliation, residual
        detection) have to use this instead of get_position().
        """
        if not (self._client and self._connected):
            raise RuntimeError("broker not connected; position state unknown")
        try:
            pos = self._client.get_position(symbol)
        except Exception as exc:
            if self._is_no_position_error(exc):
                return None
            raise
        if pos is None:
            return None
        return {
            "symbol": pos.symbol,
            "qty": float(pos.qty),
            "market_value": float(pos.market_value),
            "cost_basis": float(pos.cost_basis),
            "unrealized_pl": float(pos.unrealized_pl),
            "unrealized_plpc": float(pos.unrealized_plpc),
            "avg_entry_price": float(pos.avg_entry_price),
        }

    def get_order(self, order_id: str):
        """Fetch a single order by broker id (adapter contract)."""
        if not (self._client and self._connected):
            raise RuntimeError("broker not connected")
        getter = (getattr(self._client, "get_order_by_id", None)
                  or getattr(self._client, "get_order", None))
        if getter is None:
            raise RuntimeError("broker client exposes no order lookup")
        return getter(order_id)

    def get_order_by_client_id(self, client_order_id: str):
        """Fetch an order by our own idempotency key (adapter contract).

        This is what makes an interrupted submit recoverable: the key is ours,
        so we can ask the broker whether the order actually landed.
        """
        if not (self._client and self._connected):
            raise RuntimeError("broker not connected")
        getter = getattr(self._client, "get_order_by_client_id", None)
        if getter is None:
            raise RuntimeError("broker client cannot look up by client id")
        try:
            return getter(client_order_id)
        except Exception as exc:
            if self._is_no_position_error(exc) or "not found" in str(exc).lower():
                return None
            raise

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get current position for a symbol, if any.

        Returns None both when flat and when the broker cannot be reached.
        Use get_position_strict() where that difference matters.
        """
        if self._client and self._connected:
            try:
                pos = self._client.get_position(symbol)
                return {
                    "symbol": pos.symbol,
                    "qty": float(pos.qty),
                    "market_value": float(pos.market_value),
                    "cost_basis": float(pos.cost_basis),
                    "unrealized_pl": float(pos.unrealized_pl),
                    "unrealized_plpc": float(pos.unrealized_plpc),
                    "avg_entry_price": float(pos.avg_entry_price),
                }
            except Exception:
                return None
        return None


# ---------------------------------------------------------------------------
# Trading Engine — Orchestrator
# ---------------------------------------------------------------------------
class _CapturingAdapter:
    """Passes an order through while keeping the broker's own reply.

    ExecutionSafety.submit() maps the broker result into a ledger record and
    returns that; the caller still needs the raw ExecutionResult -- latency,
    error text, status. Created per call rather than kept on the shared
    adapter, so two concurrent entries on different symbols cannot overwrite
    each other's answer.
    """

    def __init__(self, inner):
        self._inner = inner
        self.result = None

    def submit_order(self, **kwargs):
        self.result = self._inner.submit_order(**kwargs)
        return self.result

    def __getattr__(self, name):
        return getattr(self._inner, name)


@contextlib.contextmanager
def _null_claim():
    """No position truth means no lock to take; the caller is already alone."""
    yield


class TradingEngine:
    """
    High-level trading engine that:

    1. Receives signals from Sentiment & Pattern engines
    2. Evaluates combined conviction and risk
    3. Executes trades via AlpacaBroker (or simulation)
    4. Tracks position sizes and risk limits
    5. Routes binary option signals to PocketOptionBroker
    """

    def __init__(
        self,
        alpaca_broker: Optional[AlpacaBroker] = None,
        pocket_option: Optional[PocketOptionBroker] = None,
        max_position_size: float = DEFAULT_MAX_POSITION_SIZE,
        risk_per_trade: float = DEFAULT_RISK_PER_TRADE,
        simulate: bool = False,
        max_total_exposure: float = DEFAULT_MAX_TOTAL_EXPOSURE,
        max_concurrent_positions: int = DEFAULT_MAX_CONCURRENT_POSITIONS,
    ):
        self.broker = alpaca_broker or AlpacaBroker(simulate=simulate)
        self.pocket = pocket_option or PocketOptionBroker()
        # Single authority over "am I exposed / may I trade this symbol".
        # Built lazily so simulation and tests need no ledger on disk.
        self._position_truth = None
        self._journal = None

        self.max_position_size = max_position_size
        self.risk_per_trade = risk_per_trade

        self.max_total_exposure = max_total_exposure
        self.max_concurrent_positions = max_concurrent_positions

        if self.max_position_size > 0.50:
            logger.critical("Portfolio cap per symbol cannot exceed 50%!")
            raise AssertionError("Portfolio cap per symbol cannot exceed 50%!")
        if self.max_total_exposure < self.max_position_size:
            logger.warning(
                "Total exposure cap (%.0f%%) is below the per-position cap "
                "(%.0f%%); the per-position cap can never be reached.",
                self.max_total_exposure * 100, self.max_position_size * 100,
            )

        # Trade history (in-memory for recent tracking)
        self._trade_log: List[dict] = []

        # Cooldown tracking to avoid re-trading same symbol too fast
        self._last_trade: Dict[str, float] = {}

        # Reference price tracking — set by execute()/_calculate_quantity().
        # Reset each cycle by the orchestrator (before Step 1).
        self.ref_price_failed: bool = False
        self.ref_price_succeeded: bool = False

        logger.debug(
            "TradingEngine initialised (simulate=%s, broker=%s)",
            self.broker.is_simulating,
            "Alpaca" if not self.broker.is_simulating else "SIMULATION",
        )

    # ------------------------------------------------------------------
    # Signal Execution
    # ------------------------------------------------------------------
    @property
    def journal(self):
        """Decision journal. Never allowed to break trading if unavailable."""
        if self._journal is None:
            try:
                from decision_log import DecisionLog
                self._journal = DecisionLog()
            except Exception:
                self._journal = False
        return self._journal or None

    @property
    def position_truth(self):
        """Lazily built PositionTruth over broker + order ledger + state file."""
        if self._position_truth is None:
            try:
                from execution_safety import (
                    BrokerExecutionAdapter, ExecutionSafety, PositionTruth,
                )
                # The order ledger belongs in the environment-segregated data
                # directory, like every other store. It defaulted to
                # `backend/data/execution_ledger.json` -- inside the source
                # tree, identical for paper and live -- so the two would have
                # shared one order history, silently, contradicting the
                # segregation guarantee everything else honours. Nothing set
                # EXECUTION_LEDGER_PATH outside the tests, so that hardcoded
                # path was the one in use.
                ledger = os.environ.get("EXECUTION_LEDGER_PATH")
                if not ledger:
                    ledger = os.path.join(resolve_data_dir(),
                                          "execution_ledger.json")
                    # Moving a default orphans whatever the old one holds.
                    # Say so rather than leaving real order history stranded
                    # in a directory nothing reads any more.
                    legacy = os.path.join(
                        os.path.dirname(os.path.abspath(__file__)),
                        "data", "execution_ledger.json")
                    if os.path.exists(legacy) and legacy != ledger:
                        logger.warning(
                            "An execution ledger exists at the OLD default "
                            "%s. It is no longer read. If it holds real "
                            "orders, move it to %s before trading.",
                            legacy, ledger)
                safety = ExecutionSafety(ledger)
                self._position_truth = PositionTruth(
                    safety, BrokerExecutionAdapter(self.broker))
            except Exception as exc:
                logger.error(
                    "PositionTruth unavailable (%s). Entry gating will fail "
                    "closed until this is resolved.", exc,
                )
                self._position_truth = False
        return self._position_truth or None

    def _refuse(self, signal: TradeSignal, blocker: str, detail: str) -> ExecutionResult:
        """Refuse an entry, and RECORD why.

        Every early return in execute() used to build its own ExecutionResult,
        and five of the seven never touched the journal. A refusal with no
        record is indistinguishable from "no signal was generated" -- which is
        precisely the ambiguity the journal exists to remove. Routing them all
        through here makes a silent refusal impossible to write by accident.
        """
        logger.info("Entry refused for %s: %s", signal.symbol, detail)
        if self.journal:
            try:
                self.journal.blocked(
                    signal.symbol,
                    signal.side.value if signal.side else "",
                    blocker=blocker, detail=detail,
                    inputs={"conviction": getattr(signal, "conviction", None),
                            "source": getattr(signal, "source", ""),
                            "reason": getattr(signal, "reason", "")},
                )
            except Exception:
                pass
        return ExecutionResult(
            success=False, symbol=signal.symbol, side=signal.side, quantity=0,
            filled_price=None, filled_qty=0, order_id=None,
            status=OrderStatus.REJECTED, latency_ms=0, error=detail,
        )

    def execute(self, signal: TradeSignal, overnight_risk: bool = False) -> ExecutionResult:
        """
        Evaluate and execute a trade signal.

        This is the main entry point for integrating Sentiment and
        Pattern engine outputs.

        Args:
            signal: A TradeSignal from the combined engines.

        Returns:
            ExecutionResult with fill details.
        """
        # 1. Validate signal
        if not signal.is_actionable:
            return self._refuse(
                signal, "not actionable",
                f"Signal not actionable: action={signal.action}, "
                f"conviction={signal.conviction:.3f}")

        # 2. Check cooldown (5 minutes between trades on same symbol)
        cooldown_s = ENTRY_COOLDOWN_S
        # In-memory OR ledger, whichever is later. The in-memory map is fast
        # but dies with the process; the ledger survives restarts. Taking the
        # max means a crash-restart loop cannot use a cleared cooldown to
        # re-enter a symbol it just traded.
        last_time = self._last_trade.get(signal.symbol, 0)
        truth = self.position_truth
        if truth is not None:
            try:
                persisted = truth.safety.last_entry_time(signal.symbol)
                if persisted:
                    last_time = max(last_time, persisted)
            except Exception:
                pass
        if time.time() - last_time < cooldown_s:
            remaining = int(cooldown_s - (time.time() - last_time))
            return self._refuse(
                signal, "cooldown",
                f"Cooldown active for {signal.symbol} ({remaining}s remaining)")

        # 2b. Single-authority exposure gate. Blocks on already-exposed,
        #     unresolved orders, unknown broker state, a kill switch, or any
        #     disagreement between the order ledger and the position file.
        #     Simulation has no real exposure to protect, so it is exempt.
        # The exposure check and the order that acts on it must not be
        # separated: can_enter() READS exposure, execute_order() WRITES it.
        # Measured with 12 concurrent attempts on one symbol before this
        # existed: 5 orders reached the broker, 50 shares against an
        # intended 10. The claim is per symbol, released in `finally`.
        _entry_claim = None
        try:
            if not self.broker.is_simulating:
                truth = self.position_truth
                if truth is None:
                    return self._refuse(
                        signal, "position truth unavailable",
                        "Position truth unavailable — refusing to trade")
                side_text = signal.side.value if signal.side else "buy"

                # Execution-quality gate: a symbol whose recent fills cost more
                # than the budget is refused until its rolling average recovers.
                # Per symbol, because slippage is a property of the instrument --
                # halting SPY because GLD is expensive throws away good trades.
                if self.journal:
                    ok, why = self.journal.symbol_is_tradeable(
                        signal.symbol,
                        (signal.take_profit_pct or DEFAULT_TAKE_PROFIT_PCT) * 100.0)
                    if not ok:
                        logger.warning("Entry blocked for %s: %s", signal.symbol, why)
                        self.journal.blocked(
                            signal.symbol, side_text,
                            blocker="execution cost", detail=why)
                        return ExecutionResult(
                            success=False, symbol=signal.symbol,
                            side=signal.side, quantity=0,
                            filled_price=None, filled_qty=0,
                            order_id=None, status=OrderStatus.REJECTED,
                            latency_ms=0, error="Entry blocked: %s" % why,
                        )

                _entry_claim = truth.entry_claim(signal.symbol, side_text)
                allowed, reason = _entry_claim.__enter__()
                if not allowed:
                    logger.warning(
                        "Entry blocked for %s (%s): %s", signal.symbol, side_text, reason,
                    )
                    if self.journal:
                        self.journal.blocked(
                            signal.symbol, side_text,
                            blocker=reason.split(":")[0].strip(),
                            detail=reason,
                            inputs={"conviction": signal.conviction,
                                    "source": getattr(signal, "source", ""),
                                    "reason": getattr(signal, "reason", "")},
                        )
                    return ExecutionResult(
                        success=False, symbol=signal.symbol,
                        side=signal.side, quantity=0,
                        filled_price=None, filled_qty=0,
                        order_id=None, status=OrderStatus.REJECTED,
                        latency_ms=0,
                        error="Entry blocked: %s" % reason,
                    )

            # 3. Determine quantity
            account = self.broker.get_account()
            quantity = self._calculate_quantity(
                signal.symbol, signal.side, signal.conviction, account,
                stop_loss_pct=signal.stop_loss_pct,
                overnight_risk=overnight_risk,
            )
            try:
                quantity = bounded_signal_quantity(quantity, signal.quantity)
            except ValueError as exc:
                return self._refuse(signal, "invalid quantity override", str(exc))

            if quantity <= 0:
                return self._refuse(
                    signal, "size zero",
                    f"Calculated quantity is zero or negative "
                    f"(account equity={account.equity:.2f}) — a sizing cap, "
                    f"buying power, or an unaffordable single share")

            # 4. Execute order — attach protective bracket (stop-loss/take-profit)
            #    so the broker enforces the exits even if our process stalls.
            order_type = OrderType.LIMIT if signal.limit_price else OrderType.MARKET
            try:
                ref_price = self._get_reference_price(signal.symbol)
                self.ref_price_succeeded = True
            except Exception as e:
                self.ref_price_failed = True
                logger.warning(
                    "Reference price unavailable for %s, skipping trade: %s",
                    signal.symbol, e,
                )
                return self._refuse(
                    signal, "no reference price",
                    f"Reference price unavailable: {e}")
            result = self._submit_entry(
                signal, quantity, order_type, ref_price)
        finally:
            if _entry_claim is not None:
                _entry_claim.__exit__(None, None, None)

        # 5. Update cooldown on success
        if result.success:
            self._last_trade[signal.symbol] = time.time()

            # 5a. Persist basic position info to state file (best-effort).
            #     Full metadata (pattern_id, strategy, etc.) is added later
            #     via persist_filled_trade() from the orchestrator.
            # Record what the BROKER actually filled, never what we asked for.
            # Using the requested quantity overstates the position on a partial
            # fill, and falling back to the reference price would record an
            # entry we never paid -- which then feeds P&L and the learner.
            filled_qty = int(result.filled_qty or 0)
            if filled_qty <= 0 or not result.filled_price:
                logger.warning(
                    "Not persisting %s: broker reported no confirmed fill "
                    "(filled_qty=%s, filled_price=%s). Reconciliation will "
                    "pick it up.",
                    signal.symbol, result.filled_qty, result.filled_price,
                )
                filled_qty = 0
            if filled_qty > 0 and self.journal:
                used_notional, open_count = self.current_exposure(account)
                self.journal.entered(
                    signal.symbol,
                    signal.side.value if signal.side else "buy",
                    filled_qty, float(result.filled_price or 0.0),
                    reason=getattr(signal, "reason", "") or "signal",
                    inputs={"conviction": round(signal.conviction, 4),
                            "source": getattr(signal, "source", ""),
                            "stop_loss_pct": signal.stop_loss_pct,
                            "take_profit_pct": signal.take_profit_pct,
                            "reference_price": ref_price},
                    gates={"exposure_gate": "passed",
                           "open_positions_before": open_count,
                           "notional_before": round(used_notional, 2)},
                    sizing={"requested_qty": quantity,
                            "filled_qty": filled_qty,
                            "equity": round(account.equity, 2),
                            "risk_per_trade": self.risk_per_trade,
                            "max_position_size": self.max_position_size,
                            "max_total_exposure": self.max_total_exposure},
                    trade_id=str(result.order_id or ""),
                )
            if filled_qty > 0:
                _persist_open_position(
                    symbol=signal.symbol,
                    qty=filled_qty,
                    side=signal.side.value if signal.side else "buy",
                    entry_price=result.filled_price,
                    entry_time=time.time(),
                    broker_order_id=result.order_id,
                    stop_loss=result.stop_loss_price,
                    take_profit=result.take_profit_price,
                    conviction=signal.conviction,
                )

        # 6. Log the trade
        self._trade_log.append({
            "timestamp": time.time(),
            "signal": signal,
            "result": result.dict(),
        })

        return result

    def _submit_entry(self, signal, quantity: int, order_type, ref_price):
        """Place an entry THROUGH the order ledger.

        Entries used to call broker.execute_order() directly. Exits went
        through the ledger; entries did not. Verified by running one and
        querying the table: `client_order_id sent: <ABSENT>`, `ledger rows
        after entry: 0`. Three safeguards were reading an empty table:

          * No idempotency key, so a submit the broker acknowledged but we
            never saw the answer to -- a timeout, a dropped connection --
            could be retried into a second real position. The entry claim
            stops two SIMULTANEOUS entries; it does nothing about one order
            accepted twice.
          * can_enter()'s "unresolved order outstanding" check was blind on
            the entry side, because no entry row was ever written.
          * last_entry_time() queries `WHERE is_exit=0`. Those rows did not
            exist, so it always returned None and the PERSISTED cooldown --
            the one meant to survive a restart -- silently fell back to an
            in-memory value that a restart clears.

        The ledger reserves the intent before the broker call and commits the
        broker's own answer after, so an interrupted submit is recoverable
        rather than invisible.
        """
        truth = self.position_truth
        adapter = getattr(truth, "adapter", None) if truth is not None else None
        if truth is None or adapter is None:
            # Simulation, or a caller that never wired position truth. There
            # is no ledger to write to; behave exactly as before.
            return self.broker.execute_order(
                symbol=signal.symbol, side=signal.side, quantity=quantity,
                order_type=order_type, limit_price=signal.limit_price,
                stop_loss_pct=signal.stop_loss_pct,
                take_profit_pct=signal.take_profit_pct,
                reference_price=ref_price)

        side_text = signal.side.value if signal.side else "buy"
        # Distinct per attempt: this key is what makes a RETRY idempotent, not
        # what deduplicates two independent signals. Two signals for the same
        # symbol are the entry claim's problem, and it holds the symbol.
        entry_key = "entry:%s:%s:%d" % (
            signal.symbol, side_text, int(time.time() * 1000))

        capture = _CapturingAdapter(adapter)
        try:
            record = truth.safety.submit(
                capture,
                client_order_key=entry_key,
                symbol=signal.symbol,
                side=side_text,
                quantity=quantity,
                order_type=order_type,
                limit_price=signal.limit_price,
                stop_loss_pct=signal.stop_loss_pct,
                take_profit_pct=signal.take_profit_pct,
                reference_price=ref_price,
            )
        except Exception as exc:
            logger.error("Entry ledger submit failed for %s: %s",
                         signal.symbol, exc)
            return ExecutionResult(
                success=False, symbol=signal.symbol, side=signal.side,
                quantity=0, filled_price=None, filled_qty=0, order_id=None,
                status=OrderStatus.REJECTED, latency_ms=0,
                error="Entry ledger unavailable: %s" % exc)

        if capture.result is not None:
            # The broker was actually called; its own answer is authoritative.
            return capture.result

        # The ledger refused before reaching the broker (kill switch, or a
        # duplicate key). Report the refusal rather than a phantom fill.
        return ExecutionResult(
            success=False, symbol=signal.symbol, side=signal.side,
            quantity=0, filled_price=None, filled_qty=0, order_id=None,
            status=OrderStatus.REJECTED, latency_ms=0,
            error="Entry refused by the order ledger: %s"
                  % (getattr(record, "error", None) or record.status),
            details={"ledger_status": record.status,
                     "client_order_key": entry_key})

    def close_position_guarded(self, symbol: str, reason: str = "manual"):
        """Close a position through the order ledger. The ONLY safe exit path.

        Calling broker.close_position() directly is unguarded: nothing stops a
        second close being submitted while the first is still working, and on a
        long that does not merely flatten you -- it opens a short. Exits happen
        under stress (stop-loss, drawdown breach, shutdown), which is exactly
        when duplicates are most likely.

        Sequence: refuse if an exit is already in flight -> register the intent
        -> submit -> confirm flat -> resolve the ledger record.
        """
        truth = self.position_truth
        safety = truth.safety if truth is not None else None

        # Hold the symbol across check -> register -> submit. Without it, ten
        # concurrent exit requests submitted six closes: against a 10-share
        # long that is 60 shares sold, leaving a 50-share SHORT. The monitor,
        # the daily-loss flatten and the kill switch can all fire together.
        claim = (truth.exit_claim(symbol)
                 if truth is not None and hasattr(truth, "exit_claim")
                 else _null_claim())
        with claim:
            return self._close_position_locked(symbol, reason, truth)

    def _close_position_locked(self, symbol: str, reason: str, truth):
        """The guarded close itself. Always called holding the exit claim.

        `truth` is passed rather than just `safety` because the resolution
        step needs `truth.adapter` to confirm the position is actually flat.
        """
        safety = truth.safety if truth is not None else None
        if safety is not None and safety.has_unresolved_exit(symbol):
            logger.warning(
                "Exit for %s refused: a close is already in flight (%s).",
                symbol, reason,
            )
            return ExecutionResult(
                success=False, symbol=symbol, side=None, quantity=0,
                filled_price=None, filled_qty=0, order_id=None,
                status=OrderStatus.REJECTED, latency_ms=0,
                error="Exit already in flight for %s" % symbol,
                details={"reason": reason, "duplicate_exit_prevented": True},
            )

        exit_key = "exit:%s:%s:%d" % (symbol, reason, int(time.time() * 1000))
        # Keep the SIGN. A position's quantity is negative when short, and that
        # sign is the only thing that says which way the exit goes: closing a
        # long is a SELL, closing a short is a BUY. `abs()` here threw that
        # away and the exit was registered as `side="sell"` unconditionally,
        # so every short exit went into the ledger backwards -- the same
        # direction-blindness that made the learner invert every short trade.
        signed_qty = 0
        position_known = False
        try:
            position = self.broker.get_position(symbol) or {}
            signed_qty = int(float(position.get("qty", 0) or 0))
            position_known = True
        except Exception as exc:
            logger.warning(
                "Could not read %s position before exit (%s). Direction is "
                "unknown; recording the ledger side conservatively.",
                symbol, exc)

        held_qty = abs(signed_qty)
        # Short positions are closed by buying. When the position could not be
        # read we cannot know, so assume the common case and say so in the log
        # rather than recording a confident wrong answer silently.
        exit_side = "buy" if signed_qty < 0 else "sell"
        if not position_known:
            logger.warning(
                "Exit for %s registered as '%s' without confirming direction.",
                symbol, exit_side)

        if safety is not None:
            try:
                safety.register_exit(
                    client_order_key=exit_key, symbol=symbol,
                    side=exit_side, quantity=max(1, held_qty),
                )
            except Exception as exc:
                logger.error(
                    "Could not register exit intent for %s: %s — refusing to "
                    "close unguarded.", symbol, exc,
                )
                return ExecutionResult(
                    success=False, symbol=symbol, side=None, quantity=0,
                    filled_price=None, filled_qty=0, order_id=None,
                    status=OrderStatus.REJECTED, latency_ms=0,
                    error="Exit ledger unavailable: %s" % exc,
                    details={"reason": reason},
                )

        result = self.broker.close_position(symbol)

        if safety is not None:
            try:
                state = "unknown"
                if truth is not None:
                    state = safety.position_state(truth.adapter, symbol)
                data = safety._read()
                raw = data["orders"].get(exit_key)
                if raw is not None:
                    record = safety._record_from_raw(raw)
                    if state == "flat":
                        record.status, record.error = "filled", None
                    elif state == "open":
                        record.status, record.error = "residual", "broker_position_not_flat"
                    else:
                        record.status, record.error = "residual", "broker_position_unknown"
                    record.revision += 1
                    record.updated_at = time.time()
                    with safety._locked():
                        data = safety._read()
                        data["orders"][exit_key] = _dataclass_asdict(record)
                        safety._write(data)
                    result.details = dict(result.details or {})
                    result.details.update(
                        {"exit_key": exit_key, "position_after": state,
                         "reason": reason}
                    )
                    if self.journal:
                        self.journal.exited(
                            symbol, "", trigger=reason,
                            entry_price=0.0, exit_price=0.0, profit_pct=0.0,
                            quantity=held_qty, trade_id=exit_key,
                            detail="position_after=%s" % state,
                        )
            except Exception as exc:
                logger.error(
                    "Exit for %s submitted but ledger update failed: %s. "
                    "Reconciliation will resolve it.", symbol, exc,
                )
        return result

    def persist_filled_trade(
        self,
        symbol: str,
        pattern_id,
        strategy: str,
        regime: str,
    ) -> None:
        """
        Update the persisted position state with additional metadata that is
        only available after the pattern engine has tracked the trade.

        Called by the orchestrator after record_trade_pattern_and_track().
        Best-effort — never raises.
        """
        try:
            state = _get_position_state()
            positions = state.load_positions()
            for pos in positions:
                if pos.get("symbol") == symbol:
                    pos["position_id"] = pattern_id
                    pos["strategy"] = strategy
                    pos["regime_at_entry"] = regime
                    break
            state.save_positions(positions)
        except Exception as e:
            logger.warning(
                "Failed to update persisted trade metadata for %s: %s",
                symbol, e,
            )

    def get_broker_positions(self) -> List[dict]:
        """Fetch positions from Alpaca; raise on broker/query failure.

        An empty list is a valid broker response only when the API succeeds.
        """
        if not self.broker or self.broker.is_simulating:
            raise BrokerPositionError("broker positions unavailable in simulation mode")
        client = getattr(self.broker, "_client", None)
        if client is None or not self.broker._connected:
            initialization_error = getattr(self.broker, "_initialization_error", None)
            if initialization_error is not None:
                raise BrokerPositionError(
                    "Alpaca broker initialization failed"
                ) from initialization_error
            raise BrokerPositionError("Alpaca broker is not connected")
        try:
            positions = client.get_all_positions()
        except Exception as exc:
            logger.error("Failed to fetch broker positions: %s", exc)
            raise BrokerPositionError("broker position query failed") from exc
        return [{
            "symbol": p.symbol,
            "qty": float(p.qty),
            "avg_entry_price": float(p.avg_entry_price),
            "market_value": float(p.market_value),
            "unrealized_pl": float(p.unrealized_pl),
            "unrealized_plpc": float(p.unrealized_plpc),
            "asset_id": p.asset_id,
            "side": getattr(p, "side", None),
        } for p in positions]
    # ------------------------------------------------------------------
    # Position Sizing
    # ------------------------------------------------------------------
    @staticmethod
    def current_exposure(account: AccountInfo) -> tuple:
        """(total_notional, position_count) from the broker's own position list.

        Uses absolute market value so a short counts as exposure rather than
        offsetting a long -- for risk purposes we care about gross size, not
        net direction.
        """
        total = 0.0
        count = 0
        for position in (getattr(account, "positions", None) or []):
            try:
                value = position.get("market_value") if isinstance(position, dict) \
                    else getattr(position, "market_value", 0)
                total += abs(float(value or 0))
                count += 1
            except (TypeError, ValueError):
                continue
        return total, count

    def remaining_exposure_budget(self, account: AccountInfo) -> float:
        """Notional still available under the portfolio ceiling."""
        equity = float(getattr(account, "equity", 0) or 0)
        if equity <= 0:
            return 0.0
        used, _ = self.current_exposure(account)
        return max(0.0, equity * self.max_total_exposure - used)

    def _calculate_quantity(
        self, symbol: str, side: Optional[OrderSide], conviction: float,
        account: AccountInfo, stop_loss_pct: Optional[float] = None,
        overnight_risk: bool = False,
    ) -> int:
        """
        Calculate the number of shares to trade based on flat fractional sizing:
        - Portfolio equity
        - Risk per trade (fixed %, no conviction scaling)
        - Max position size limit
        """
        equity = account.equity
        if equity <= 0:
            return 0

        # Flat fractional sizing: risk_amount is the amount we are willing to lose on this trade
        risk_amount = equity * self.risk_per_trade

        # Sl pct: use signal's stop_loss_pct if provided, otherwise DEFAULT_STOP_LOSS_PCT
        sl_pct = stop_loss_pct if stop_loss_pct else DEFAULT_STOP_LOSS_PCT
        if sl_pct <= 0:
            sl_pct = DEFAULT_STOP_LOSS_PCT

        # Positions carried overnight are sized against gap risk instead. The
        # position value is risk_amount / stop, so a WIDER assumed stop yields
        # a SMALLER position -- which is the point: the same dollar risk, but
        # measured against the loss that can actually happen.
        overnight_scale = 1.0
        if overnight_risk and OVERNIGHT_RISK_PCT > sl_pct:
            # Scale the CONCENTRATION cap by the same ratio, not just the risk
            # divisor. At the default settings the per-position cap binds
            # before the risk budget does, so adjusting only the divisor would
            # leave the position size completely unchanged -- protection that
            # exists in the code and not in the book.
            overnight_scale = sl_pct / OVERNIGHT_RISK_PCT
            logger.info(
                "%s: sizing for overnight gap risk — stop %.2f%% -> %.2f%%, "
                "cap scaled to %.0f%% of normal",
                symbol, sl_pct * 100, OVERNIGHT_RISK_PCT * 100,
                overnight_scale * 100,
            )
            sl_pct = OVERNIGHT_RISK_PCT

        # Position value (notional size) = risk_amount / stop_loss_pct
        position_value = risk_amount / sl_pct

        # Cap at max_position_size of portfolio
        max_value = equity * self.max_position_size * overnight_scale
        position_value = min(position_value, max_value)

        # Portfolio-level ceilings. Correlated symbols would otherwise stack
        # into a single oversized directional bet, one 15% slice at a time.
        used_notional, open_count = self.current_exposure(account)
        if self.max_concurrent_positions > 0 and open_count >= self.max_concurrent_positions:
            logger.info(
                "Skipping %s: already holding %d positions (limit %d).",
                symbol, open_count, self.max_concurrent_positions,
            )
            return 0
        remaining = max(0.0, equity * self.max_total_exposure - used_notional)
        if remaining <= 0:
            logger.info(
                "Skipping %s: portfolio exposure %.0f%% is at the %.0f%% ceiling.",
                symbol, used_notional / equity * 100, self.max_total_exposure * 100,
            )
            return 0
        position_value = min(position_value, remaining)

        # Convert to shares using a reference price
        try:
            ref_price = self._get_reference_price(symbol)
            self.ref_price_succeeded = True
        except Exception as e:
            self.ref_price_failed = True
            logger.warning(
                "Reference price unavailable for %s, skipping quantity calculation: %s",
                symbol, e,
            )
            return 0
        if ref_price <= 0:
            return 0

        quantity = int(position_value / ref_price)

        # Do NOT floor this at 1 share. A single share of an expensive symbol
        # can be worth several times the per-position cap on a small account
        # (e.g. a $900 share against a $300 cap is 45% of a $2,000 account,
        # not 15%). If we cannot afford even one share within the cap, the
        # correct size is zero.
        if quantity < 1:
            logger.info(
                "Skipping %s: one share ($%.2f) exceeds the per-position cap "
                "($%.2f at %.0f%% of $%.2f equity).",
                symbol, ref_price, max_value, self.max_position_size * 100, equity,
            )
            return 0

        # Never order more than the account can actually settle.
        buying_power = float(getattr(account, "buying_power", 0) or 0)
        if buying_power > 0:
            affordable = int(buying_power / ref_price)
            if affordable < quantity:
                logger.info(
                    "Reducing %s from %d to %d shares: buying power $%.2f.",
                    symbol, quantity, affordable, buying_power,
                )
                quantity = affordable
            if quantity < 1:
                return 0

        return quantity

    @staticmethod
    def _get_reference_price(symbol: str) -> float:
        """Get a reference price for the symbol."""
        try:
            from alpaca.data.requests import StockLatestQuoteRequest
            from alpaca.data.historical import StockHistoricalDataClient

            key = os.environ.get("APCA_API_KEY_ID", "")
            secret = os.environ.get("APCA_API_SECRET_KEY", "")
            if key and secret:
                client = StockHistoricalDataClient(key, secret)
                quote = client.get_stock_latest_quote(
                    StockLatestQuoteRequest(symbol_or_symbols=[symbol])
                )
                if symbol in quote:
                    price = quote[symbol].ask_price or quote[symbol].bid_price
                    if price and price > 0:
                        return float(price)
                    raise RuntimeError(
                        f"Symbol {symbol} has zero/empty price in Alpaca quote"
                    )
                raise RuntimeError(
                    f"Symbol {symbol} not found in Alpaca quote response"
                )
        except Exception as e:
            # Re-raise symbol-specific RuntimeErrors unchanged so the
            # message stays clean (e.g. "Symbol SPY not found" rather
            # than "Failed to fetch reference price for SPY from Alpaca:
            # Symbol SPY not found").
            if isinstance(e, RuntimeError):
                raise
            raise RuntimeError(
                f"Failed to fetch reference price for {symbol} from Alpaca: {e}"
            ) from e

    # ------------------------------------------------------------------
    # Strategy — combined execution pipeline
    # ------------------------------------------------------------------
    def evaluate_and_execute(
        self,
        symbol: str,
        sentiment_conviction: float,
        pattern_signal: Optional[Any] = None,
        sentiment_reason: str = "",
        force_binary: bool = False,
    ) -> ExecutionResult:
        """
        Convenience method: run the full evaluation → execution pipeline.

        Args:
            symbol: Trading symbol (e.g., "SPY")
            sentiment_conviction: Conviction score from Sentiment Engine
            pattern_signal: EvaluationSignal from Pattern Engine (optional)
            sentiment_reason: Reason string from Sentiment Engine
            force_binary: If True, route to Pocket Option (placeholder)

        Returns:
            ExecutionResult
        """
        signal = TradeSignal.from_engines(
            symbol=symbol,
            sentiment_conviction=sentiment_conviction,
            pattern_signal=pattern_signal,
            sentiment_reason=sentiment_reason,
        )

        if force_binary:
            # Route to Pocket Option (placeholder)
            pocket_result = self.pocket.place_binary_option(
                symbol=symbol,
                direction="call" if signal.conviction > 0 else "put",
                amount=100.0,
                expiry_minutes=5,
            )
            return ExecutionResult(
                success=pocket_result.get("status") == "demo_placed",
                symbol=symbol, side=signal.side, quantity=0,
                filled_price=None, filled_qty=0,
                order_id=pocket_result.get("trade_id"),
                status=OrderStatus.PENDING,
                latency_ms=0,
                details={
                    "pocket_option": pocket_result,
                    "signal_source": signal.source,
                },
            )

        return self.execute(signal)

    # ------------------------------------------------------------------
    # Trade History
    # ------------------------------------------------------------------
    def get_recent_trades(self, limit: int = 20) -> List[dict]:
        """Return the most recent trade results."""
        return [
            {
                "timestamp": t["timestamp"],
                "symbol": t["signal"].symbol,
                "action": t["signal"].action,
                "conviction": t["signal"].conviction,
                "source": t["signal"].source,
                "reason": t["signal"].reason[:120],
                **t["result"],
            }
            for t in self._trade_log[-limit:]
        ]

    def summary(self) -> dict:
        """Return a summary of engine state."""
        account = self.broker.get_account()
        recent = self.get_recent_trades(5)

        return {
            "mode": "simulation" if self.broker.is_simulating else "live",
            "account": {
                "buying_power": account.buying_power,
                "equity": account.equity,
                "positions": len(account.positions),
            },
            "recent_trades": len(self._trade_log),
            "active_cooldowns": {
                sym: round(time.time() - t, 1)
                for sym, t in self._last_trade.items()
            },
            "last_5_trades": recent,
        }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    import sys

    is_json = "--json" in sys.argv
    log_level = logging.WARNING if is_json else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s %(message)s")

    engine = TradingEngine(simulate=True)

    if "--summary" in sys.argv:
        s = engine.summary()
        if is_json:
            print(json.dumps(s))
            sys.exit(0)
        print("\n=== Trading Engine Summary ===")
        print(f"  Mode:            {s['mode']}")
        print(f"  Buying Power:    ${s['account']['buying_power']:,.2f}")
        print(f"  Equity:          ${s['account']['equity']:,.2f}")
        print(f"  Open Positions:  {s['account']['positions']}")
        print(f"  Trades Executed: {s['recent_trades']}")
        print(json.dumps(s, indent=2))

    if "--evaluate" in sys.argv and len(sys.argv) >= 4:
        # Usage: python trading.py --evaluate SPY 0.65
        symbol = sys.argv[2]
        conviction = float(sys.argv[3])
        pattern_signal = None

        # Try to also load pattern signal if patterns.db exists
        try:
            from patterns import PatternEngine
            pe = PatternEngine()
            pattern_signal = pe.evaluate(
                symbol=symbol,
                sentiment_score=conviction,
                conviction_score=conviction,
                rsi_value=50.0,
                ema_short=500.0,
                ema_long=498.0,
            )
        except Exception:
            pass

        result = engine.evaluate_and_execute(
            symbol=symbol,
            sentiment_conviction=conviction,
            pattern_signal=pattern_signal,
        )

        print(f"\n=== Trade Execution: {symbol} ===")
        print(f"  Side:    {result.side.value if result.side else 'N/A'}")
        print(f"  Qty:     {result.quantity}")
        print(f"  Fill:    {result.filled_price}")
        print(f"  Status:  {result.status.value}")
        print(f"  Latency: {result.latency_ms:.0f}ms")
        print(f"  Error:   {result.error or 'None'}")

        if pattern_signal:
            print(f"\n  Pattern Signal: {pattern_signal.action.upper()} "
                  f"({pattern_signal.conviction:+.3f})")

        print(f"\n  Full Result:")
        print(json.dumps(result.dict(), indent=2))

    if "--pocket" in sys.argv:
        # Test Pocket Option placeholder
        pocket = PocketOptionBroker()
        pocket.connect()
        r = pocket.place_binary_option("EURUSD", "call", 100.0, 5)
        print(f"\n=== Pocket Option Test ===")
        print(json.dumps(r, indent=2))
