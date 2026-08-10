#!/usr/bin/env python3
"""Terminal dashboard for Educated Trades.

Runs on your machine, not the server. Pure standard library — no Node, no
build step, no dependencies — so it also works over SSH, which is where you
will actually want it.

    export API_AUTH_TOKEN=...            # same token the bot uses
    python3 scripts/dashboard.py         # http://127.0.0.1:3099 by default
    python3 scripts/dashboard.py --url http://127.0.0.1:3099 --interval 5

Watching a server through an SSH tunnel (the API binds loopback on purpose):

    ssh -L 3099:127.0.0.1:3099 you@your-server
    python3 scripts/dashboard.py

Layout note: refusals are shown above P&L, deliberately. "No trades today" is
ambiguous on its own — it is either the confidence gate correctly declining
noise, or a broken signal path. Refusals with reasons tell you which, and the
P&L cannot. An empty journal during market hours means nothing reached a gate,
which is the one reading that always warrants investigation.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_URL = os.environ.get("DASHBOARD_URL", "http://127.0.0.1:3099")
DEFAULT_INTERVAL = float(os.environ.get("DASHBOARD_INTERVAL", "5"))

# --- colour, degraded gracefully when piped to a file or a dumb terminal ---
_TTY = sys.stdout.isatty() and os.environ.get("TERM", "") not in ("", "dumb")


def _c(code: str, text: str) -> str:
    return "\033[%sm%s\033[0m" % (code, text) if _TTY else text


def green(t): return _c("32", t)
def red(t): return _c("31;1", t)
def yellow(t): return _c("33", t)
def dim(t): return _c("2", t)
def bold(t): return _c("1", t)
def cyan(t): return _c("36", t)


def fetch(url: str, path: str, token: str, timeout: float = 6.0):
    """GET one endpoint. Returns (payload, error_string)."""
    request = urllib.request.Request(url.rstrip("/") + path)
    if token:
        request.add_header("Authorization", "Bearer %s" % token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return None, "401 unauthorised — check API_AUTH_TOKEN"
        return None, "HTTP %s" % exc.code
    except urllib.error.URLError as exc:
        return None, "unreachable (%s)" % getattr(exc, "reason", exc)
    except Exception as exc:                      # noqa: BLE001
        return None, str(exc)


def money(value) -> str:
    try:
        return "$%s" % format(float(value), ",.2f")
    except (TypeError, ValueError):
        return "—"


def pct(value, places=2) -> str:
    try:
        return "%+.*f%%" % (places, float(value))
    except (TypeError, ValueError):
        return "—"


def ago(timestamp) -> str:
    try:
        delta = time.time() - float(timestamp)
    except (TypeError, ValueError):
        return "—"
    if delta < 90:
        return "%ds" % int(delta)
    if delta < 5400:
        return "%dm" % int(delta // 60)
    return "%dh" % int(delta // 3600)


def flag(ok: bool, ok_text: str, bad_text: str) -> str:
    return green(ok_text) if ok else red(bad_text)


def render(url: str, token: str) -> str:
    out = []
    status, err = fetch(url, "/api/status", token)

    header = bold("EDUCATED TRADES")
    clock = datetime.now().strftime("%H:%M:%S")

    if err:
        # An unreachable API on a trading bot is itself the headline. Do not
        # bury it under empty panels that imply everything is fine.
        out.append("%s   %s" % (header, dim(clock)))
        out.append("")
        out.append(red("  API %s" % err))
        out.append("")
        out.append(dim("  The bot may still be trading — this only means the"))
        out.append(dim("  dashboard cannot see it. Check:  systemctl status educated-trades"))
        return "\n".join(out)

    orch = status.get("orchestrator", {}) or {}
    mode = str(orch.get("mode", "?")).upper()
    regime = (orch.get("market_regime") or {})
    hours = (orch.get("market_hours") or {})
    is_open = bool(hours.get("is_open"))

    portfolio, perr = fetch(url, "/api/portfolio", token)
    portfolio = portfolio or {}
    environment = str(portfolio.get("mode", "")).lower()
    env_tag = (red("● LIVE") if environment == "live"
               else cyan("○ PAPER") if environment
               else dim("○ SIM"))

    out.append("%s   %s   %s" % (header, dim(clock), env_tag))
    out.append(dim("─" * 74))

    mode_text = green(mode) if mode == "AUTONOMOUS" else yellow(mode)
    market_text = (green("open") if is_open
                   else dim(str(hours.get("phase", "closed"))))
    adx = regime.get("adx")
    regime_text = "%s%s" % (
        regime.get("regime", "unknown"),
        "" if adx is None else ", ADX %.1f" % float(adx))

    out.append("  %-9s %-22s %-9s %s" % ("MODE", mode_text, "MARKET", market_text))
    out.append("  %-9s %-22s %-9s %s" % ("EQUITY", money(portfolio.get("equity")),
                                         "REGIME", dim(regime_text)))
    day = orch.get("daily_pnl_pct", 0.0)
    day_text = green(pct(day)) if (day or 0) >= 0 else red(pct(day))
    out.append("  %-9s %-22s %-9s %s"
               % ("DAY P&L", day_text, "LIMIT",
                  dim("%.1f%%" % float(orch.get("daily_loss_limit_pct", 0) or 0))))
    out.append("  %-9s %-22s %-9s %s"
               % ("CYCLE", "#%s" % orch.get("cycle_count", 0), "UPTIME",
                  dim(ago(time.time() - float(orch.get("uptime_seconds", 0) or 0)))))

    # --- safety first, because these are the states that stop everything ---
    out.append("")
    out.append(bold("  SAFETY"))
    killed = bool(orch.get("killed") or status.get("kill_switch_active"))
    unprotected = orch.get("unprotected_positions") or {}
    preflight = orch.get("preflight") or {}
    pf_ok = preflight.get("ok", None)
    out.append("    %-16s %s" % ("kill switch", flag(not killed, "clear", "ENGAGED")))
    out.append("    %-16s %s" % ("daily loss",
                                 flag(not orch.get("daily_loss_hit"), "clear", "HIT — halted")))
    out.append("    %-16s %s" % ("drawdown",
                                 flag(not orch.get("drawdown_killed"), "clear", "KILLED")))
    out.append("    %-16s %s" % ("unprotected",
                                 flag(not unprotected, "none",
                                      "%d POSITION(S)" % len(unprotected))))
    out.append("    %-16s %s" % ("preflight",
                                 dim("—") if pf_ok is None
                                 else flag(bool(pf_ok), "passed", "FAILED")))
    if pf_ok is False:
        for reason in (preflight.get("blocking") or [])[:3]:
            out.append("      %s" % red("• %s" % reason))

    # --- positions ---
    positions = portfolio.get("positions") or []
    out.append("")
    out.append(bold("  POSITIONS (%d)" % len(positions)))
    if perr:
        out.append("    %s" % yellow(perr))
    elif not positions:
        out.append("    %s" % dim("flat"))
    else:
        for position in positions[:8]:
            plpc = position.get("unrealized_plpc")
            try:
                plpc_text = pct(float(plpc) * 100.0)
            except (TypeError, ValueError):
                plpc_text = "—"
            coloured = (green(plpc_text) if str(plpc_text).startswith("+")
                        else red(plpc_text))
            out.append("    %-6s %8s @ %-10s  %-10s %s"
                       % (position.get("symbol", "?"),
                          position.get("qty", "?"),
                          money(position.get("avg_entry_price")),
                          money(position.get("market_value")),
                          coloured))

    # --- the decision journal: why it did, or did not, trade ---
    decisions, derr = fetch(url, "/api/decisions", token)
    out.append("")
    out.append(bold("  RECENT DECISIONS") + dim("   (why it acted, and why it didn't)"))
    if derr:
        out.append("    %s" % yellow(derr))
    else:
        entries = (decisions or {}).get("entries") or []
        counts = (decisions or {}).get("counts") or {}
        if counts:
            out.append("    " + dim("  ".join("%s×%d" % (k, v)
                                              for k, v in sorted(counts.items()))))
        if not entries:
            out.append("    %s" % (red("EMPTY — nothing reached a gate") if is_open
                                   else dim("no entries")))
            if is_open:
                out.append("    %s" % dim("Every refusal is journalled, so silence "
                                          "during market hours means the"))
                out.append("    %s" % dim("signal path is broken, not that it "
                                          "declined to trade."))
        for entry in entries[-8:]:
            when = str(entry.get("iso", ""))[11:19] or "--:--:--"
            event = str(entry.get("event", "?")).upper()
            symbol = entry.get("symbol", "?")
            if event == "BLOCKED":
                label = yellow("%-8s" % "BLOCKED")
                detail = entry.get("detail") or entry.get("blocker") or ""
            elif event == "ENTERED":
                label = green("%-8s" % "ENTERED")
                detail = "%s %s @ %s" % (entry.get("side", ""),
                                         entry.get("quantity", ""),
                                         money(entry.get("price")))
            elif event == "EXITED":
                label = cyan("%-8s" % "EXITED")
                detail = "%s  %s" % (entry.get("detail", ""),
                                     pct(entry.get("profit_pct")))
            else:
                label = dim("%-8s" % event[:8])
                detail = entry.get("detail", "")
            out.append("    %s %s %-5s %s"
                       % (dim(when), label, symbol, dim(str(detail)[:44])))

    out.append("")
    out.append(dim("  ctrl-c to quit"))
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument("--once", action="store_true",
                        help="render a single frame and exit (for scripting)")
    args = parser.parse_args()

    token = os.environ.get("API_AUTH_TOKEN", "")
    if not token:
        print("API_AUTH_TOKEN is not set. The bot requires it and so does "
              "this dashboard.\n  export API_AUTH_TOKEN=...", file=sys.stderr)
        return 1

    if args.once:
        print(render(args.url, token))
        return 0

    try:
        while True:
            frame = render(args.url, token)
            # Clear and home, then draw. Repainting whole frames avoids the
            # flicker of clearing first and leaving the terminal briefly blank.
            sys.stdout.write("\033[H\033[J" if _TTY else "\n\n")
            sys.stdout.write(frame + "\n")
            sys.stdout.flush()
            time.sleep(max(1.0, args.interval))
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        return 0


if __name__ == "__main__":
    sys.exit(main())
