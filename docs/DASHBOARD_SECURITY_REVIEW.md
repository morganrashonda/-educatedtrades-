# Dashboard security review

**Date:** 2026-08-14
**Scope:** the web dashboard and its path to the trading control API
**Status:** all findings fixed and verified; two decisions open

---

## Summary

The backend was hardened first: the control API requires a bearer token, binds
loopback, and refuses to start without a token. That work held up.

The dashboard did not. Four defects were found, and **three of them were
introduced by the fix for the first one.** Moving `API_AUTH_TOKEN` off the
browser and onto the server was correct and necessary, and it converted the
dashboard into an unauthenticated proxy to the trading controls. Nothing in the
existing test suite could see that, because the checks covering the frontend
assert on source text rather than behaviour.

None of this affected the bot itself. The trading process is independent of the
dashboard and continued running correctly throughout.

| | |
|---|---|
| Findings | 4 (1 high, 2 medium, 1 low) |
| All fixed | yes |
| Python checks | 836 passing |
| End-to-end | 33 passing |
| `tsc --noEmit` | 0 errors |
| `vite build` | clean |
| New regression checks | 28 (`DH-0` … `DH-25`) |
| Negative controls | 7 of 7 fire |

---

## Method

Findings came from three sources, in increasing order of yield:

1. **Reading the source.** Found the inherited template settings.
2. **Compiling it.** `tsc --noEmit` found a defect that source review and the
   entire existing frontend test section had missed.
3. **Reading the compiled artifact.** Reading `dist/server/server.js` is what
   settled the CSRF question. The source of a dependency does not tell you what
   its runtime actually does; the bundle does.

Every fix has a **negative control**: the defect is reintroduced, the suite is
run, and the check is confirmed to fail. A regression test that has never been
observed failing is not evidence.

---

## Findings

### 1 — Cross-site request forgery on the trading controls (HIGH)

**What.** The server functions in `src/server/actions.ts` authenticate to the
bot on behalf of whoever reaches them. They hold the token; they carry no
credential of their own. Possession of the URL was authority.

Binding to `127.0.0.1` does not mitigate this. The operator's own browser is on
loopback, so any page they visit can post to it.

TanStack sends an `x-tsr-serverFn: true` header on every server-function call.
A custom header forces a CORS preflight, which an attacker cannot pass — so
this *looks* like a defence. It is not one. From the compiled runtime:

```js
const res = await action(payload);   // the function has already run
...
if (!isServerFn) return unwrapped;   // only shapes the reply
```

`action(payload)` is awaited **before** the header is consulted. Omitting the
header does not prevent execution; it only changes serialization. And omitting
it makes the request "simple", which skips the preflight entirely.

**Impact.** A page the operator visits could invoke `killSwitch` or `resetKill`.
The response is opaque, so nothing could be read — but `resetKill` **silently
disarms a kill switch the operator deliberately engaged**, which is worse than
the halt. Function IDs are in the public repository.

**Fix.** Origin validation on all four server functions. State-changing calls
require a positive same-origin signal; reads are checked more loosely, because
browsers omit `Origin` on same-origin GETs and a cross-origin reader cannot see
the response anyway.

The allowlist is built from **configuration, never from the request's own `Host`
header**. Echoing `Host` back would validate a DNS-rebinding attacker, whose
forged `Origin` and `Host` agree with each other.

**Verified.** Present in the built artifact, not merely the source:
`ForbiddenOriginError` ×5 and `sec-fetch-site` ×2 in
`dist/server/assets/actions-*.js`, allowlist compiled as
`127.0.0.1:${DASHBOARD_PORT}` and resolved at runtime. Removing any single guard
fires `DH-13`, `DH-14`, `DH-20`.

---

### 2 — Heartbeat monitor was structurally incapable of reporting (MEDIUM)

**What.** `HeartbeatStatus.tsx` declared a local `const fetchHeartbeat` wrapping
a call to `fetchHeartbeat()`. Before the token fix the import was named
`getHeartbeat`, so the names did not collide. Renaming it to route through the
server function made that call resolve to **itself**.

Unbounded recursion, every 15 seconds, swallowed by the surrounding `catch`
into `setIsAlive(false)`.

**Impact.** The heartbeat panel would have shown "not alive" permanently and
never once contacted the backend — a monitoring component that could not report
a problem, on a system whose entire premise is that it must never misreport what
it did.

**Why it survived review.** Every `FE-*` check passed on it. They assert on
source text, and the source text looked correct. `tsc` found it in one run.

**Fix.** Local renamed to `poll`. `DH-22` now scans every file in `src/` and
fails if any local declaration shadows an imported name.

---

### 3 — Dashboard bound every network interface (MEDIUM)

**What.** Both entry points carried settings inherited from a reverse-proxied
sandbox template:

- `vite.config.ts` — `host: true` (binds `0.0.0.0`) and `allowedHosts: true`,
  which disables the Host-header check that prevents a page you merely *visit*
  from driving the dev server.
- `serve.ts` — pinned `0.0.0.0` with a comment explicitly refusing to honour the
  environment, on reasoning that only applied inside the sandbox.

`bun run dev` is the documented command, so this was the common path.

**Impact.** With the token held server-side, anything on the network reaching
port 3000 got a fully authenticated control plane. The backend's loopback bind
was guarding a door beside an open window.

**Fix.** Both default to `127.0.0.1`; `DASHBOARD_HOST` widens deliberately and
warns; `strictPort` prevents silent drift to 3001, which would otherwise leave
the operator reading a stale dashboard while believing it was the one they had
just started.

---

### 4 — `serve.ts` ran `sudo` to seize its port (LOW)

**What.** The template freed port 3000 by `kill`ing whatever held it, under
`sudo`, justified by "every sandbox user has passwordless sudo."

**Impact.** On a personal machine that is a password prompt attached to killing
an unrelated process. Shipped in a public repository.

**Fix.** Removed. A busy port is now reported with the command to identify the
holder. `publish.sh` was rewritten alongside it — it used `setsid`, which macOS
does not have.

---

## What the tests missed, and why

The suite was reporting one number — "822 passed" — across two very different
kinds of evidence:

- **Backend:** checks that execute the code, under concurrency, with negative
  controls. Strong.
- **Frontend:** checks that grep the source. Weak. They would pass on a file
  that does not compile.

The heartbeat recursion is the proof. It sat inside a section written
specifically to verify the dashboard's correctness, and passed.

Two changes address this:

1. `bun run typecheck` now exists as a script separate from `build`. **`vite
   build` does not typecheck** — esbuild strips types without verifying them,
   which is why the recursion built cleanly. `DH-24` fails if the script is
   removed.
2. `DH-22` performs the shadowing check structurally rather than by grep.

A typecheck that always fails is worthless, so the ~35 pre-existing unused
imports were cleared to bring it to zero. That noise is exactly what the real
defect was hiding in.

---

## Found in passing

- **`latencyMs`** was rendered in `StatusBar.tsx` but its setter was never
  called, so it was permanently `null` and that readout has never appeared. Left
  as an explicit constant with a note — it reads as unfinished intent, not an
  accident.
- **`isPositive`** in `PerformanceChart.tsx` was computed and discarded.
- **`site/`** produced 20 type errors of its own, independently confirming it is
  superseded (see below).

---

## Residual risk

**The dashboard still has no login.** The origin check stops a *hostile page*
from driving it. It does not authenticate the human. Anyone with access to the
machine, or to the port if it is ever widened, has full control. This is
inherent to holding the token server-side, and the alternative — putting it in
the browser — is worse. Documented in `README.md` and in both config files.

**The bot API sends `Access-Control-Allow-Origin: *`.** Not exploitable on its
own, since the bearer token still guards every endpoint, but broader than a
loopback dashboard requires. Worth narrowing.

**Operational mitigation, free and complete:** do not leave the dashboard
running when you are not looking at it. It is a viewer. The bot does not depend
on it.

---

## Open decisions

### `site/` — recommend deletion

23 tracked files in a public repository. Verified before recommending:

| Evidence | |
|---|---|
| `package.json` name | `team-site-template` |
| Components | 8 fewer than `src/` |
| `actions.ts` | absent |
| `src/server/api.ts` | sends **no** `Authorization` header — 401s against the current backend |
| Typecheck | 20 errors, including a state shape missing `killSwitchActive`, `dailyPnlPct`, `equity` |
| `serve.ts` | still contains the `sudo` port-kill |

It is the pre-hardening ancestor of `src/`, and `README.md` was directing users
to it. Currently excluded from typechecking; the directory is untouched pending
this decision. `git` retains the history either way.

### Committing

27 files modified, `scripts/run.sh` and `src/server/actions.ts` untracked, one
commit unpushed. Recommend splitting into coherent commits so the security fixes
are separable from the lint cleanup.

---

## Reproducing the verification

```bash
cd backend
python3 tests/test_suite.py        # 836
python3 tests/test_end_to_end.py   # 33

cd ..
bun install
bun run typecheck                  # 0 errors — NOT implied by build
bun run build
```

Confirming the token never reaches the browser — run after a build, with your
real `.env` loaded, so the test is not vacuous:

```bash
TOK=$(grep -E '^API_AUTH_TOKEN=' .env | cut -d= -f2-)
grep -rlF "$TOK" dist/client/ && echo "LEAK" || echo "clean"
```

The token should appear in **neither** bundle: the server reads it at runtime
via `process.env`, so it is never baked into either artifact.
