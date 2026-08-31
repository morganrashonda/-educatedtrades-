# NQ/QQQ post-close observer operations

This schedule is research-only. It does not import production trading,
learning, account, order, or position modules and cannot authorize execution.

## Schedule

The macOS LaunchAgent
`com.educatedtrades.opening-nq-qqq-forward` starts at 15:25 America/Chicago
each Monday through Friday. Chicago and New York change daylight-saving time
together, so this is 16:25 ET, five minutes after the frozen 16:20 ET source
delay gate.

The calendar schedule is only a wake-up request. The observer remains the
authority and fails closed when:

- the ET time gate is not met;
- the date is a weekend or predates the forward boundary;
- Alpaca does not identify the date as a cash-market session;
- credentials or source data are unavailable;
- the singleton lock is held;
- a Databento cost, request, download, or disk-space cap would be exceeded.

Market holidays may therefore produce an explicit calendar refusal rather
than evidence. Terminal `NO_SIGNAL` and `COMPLETE` sessions are immutable and
idempotent.

## Local paths

- Wrapper: `scripts/run_opening_nq_qqq_forward.sh`
- LaunchAgent source: `scripts/com.educatedtrades.opening-nq-qqq-forward.plist`
- Installed agent: `~/Library/LaunchAgents/com.educatedtrades.opening-nq-qqq-forward.plist`
- Evidence database: `~/.educated-trades/research/opening_nq_qqq_forward.db`
- Qualifying-session raw evidence: `~/.educated-trades/research/opening_nq_qqq_forward_raw/`
- Logs: `~/.educated-trades/research/logs/`

The `.env` file is sourced without printing credentials. All observer output
is created with owner-only permissions.

## Validation

The no-network readiness check is:

```bash
scripts/run_opening_nq_qqq_forward.sh --check
```

Inspect the loaded schedule with:

```bash
launchctl print gui/$(id -u)/com.educatedtrades.opening-nq-qqq-forward
```

This schedule does not run historical catch-up and does not change a refused
session into evidence without an explicit retry.
