# Accepted-break forward observer operations

This observer is research-only. It cannot import the coordinator, inspect an
account or position, submit an order, write learning state, or enable Tier 3.

## Schedule

The proposed macOS LaunchAgent
`com.educatedtrades.opening-accepted-break-forward` wakes at 15:35
America/Chicago each Monday through Friday, which is 16:35 ET. The ten-minute
separation from the existing NQ/QQQ observer avoids intentionally launching
both paid-data jobs together.

The calendar schedule is only a wake-up request. The observer independently
refuses dates before 2026-08-20, same-day runs before 16:20 ET, non-cash
sessions, incomplete sources, over-budget requests, malformed evidence,
duplicate processes, and insufficient free disk.

## Local paths after deployment

- Wrapper: `scripts/run_opening_accepted_break_forward.sh`
- LaunchAgent source:
  `scripts/com.educatedtrades.opening-accepted-break-forward.plist`
- Installed agent:
  `~/Library/LaunchAgents/com.educatedtrades.opening-accepted-break-forward.plist`
- Evidence database:
  `~/.educated-trades/research/opening_accepted_break_forward.db`
- Logs: `~/.educated-trades/research/logs/`

The `.env` file is sourced without printing credentials. `umask 077` makes new
observer files owner-only.

## No-network readiness check

```bash
scripts/run_opening_accepted_break_forward.sh --check
```

## Schedule inspection after installation

```bash
launchctl print gui/$(id -u)/com.educatedtrades.opening-accepted-break-forward
```

The source files in this research branch are not an installed schedule. Merge,
deployment, and LaunchAgent installation remain separate explicit steps.
