/**
 * Server functions — the ONLY route from browser components to the bot API.
 *
 * `server/api.ts` holds the API token and refuses to run in a browser. Client
 * components previously imported it directly, which meant either the token
 * would be bundled into the client (readable by anyone loading the page, on an
 * API that can change mode and place trades) or every request would 401.
 *
 * These wrappers close that: the browser calls a server function, the server
 * holds the token and talks to the bot over loopback. The token never crosses
 * the network boundary, and CORS never applies because the request is
 * server-to-server.
 *
 * ---------------------------------------------------------------------------
 * CROSS-SITE REQUEST FORGERY
 *
 * Moving the token server-side has a consequence that is easy to miss: these
 * functions authenticate to the bot on behalf of WHOEVER REACHES THEM. They
 * carry no credential of their own, so possession of the URL is authority.
 *
 * Binding to 127.0.0.1 does not help. Your own browser is on loopback, so any
 * page you merely visit can post to it. TanStack's own `x-tsr-serverFn` header
 * is NOT a gate — the runtime awaits `action(payload)` and only afterwards
 * reads that header to decide how to serialize the reply. An attacker omits
 * the header, which makes the request "simple", which skips the CORS preflight
 * entirely. Verified by reading dist/server/server.js, not assumed.
 *
 * Without the check below, a hostile page could silently RESET a kill switch
 * you had deliberately engaged, and nothing would tell you.
 *
 * The allowlist is built from configuration, never from the request's own Host
 * header. Echoing Host back would validate a DNS-rebinding attacker, whose
 * forged Origin and Host agree with each other.
 */
import { createServerFn } from "@tanstack/react-start";
import { getRequest } from "@tanstack/react-start/server";
import {
  getHeartbeat as _getHeartbeat,
  getOvernightRisk as _getOvernightRisk,
  triggerKillSwitch as _triggerKillSwitch,
  resetKillSwitch as _resetKillSwitch,
} from "./api";

const DASHBOARD_PORT = process.env.DASHBOARD_PORT ?? "3000";

/** Explicit origins for a proxied deployment, comma separated. */
const ALLOWED_ORIGINS: ReadonlySet<string> = new Set(
  (process.env.DASHBOARD_ORIGIN
    ? process.env.DASHBOARD_ORIGIN.split(",")
    : [
        `http://127.0.0.1:${DASHBOARD_PORT}`,
        `http://localhost:${DASHBOARD_PORT}`,
      ]
  ).map((o) => o.trim().replace(/\/+$/, "").toLowerCase()),
);

class ForbiddenOriginError extends Error {
  constructor(detail: string) {
    super(`Refused: cross-site request to a trading control (${detail})`);
    this.name = "ForbiddenOriginError";
  }
}

/**
 * @param stateChanging true for calls that alter trading state. Those demand a
 *   positive same-origin signal; a read may proceed without one, because
 *   browsers omit Origin on same-origin GETs and a cross-origin reader cannot
 *   see the response anyway.
 */
function assertSameOrigin(stateChanging: boolean): void {
  // Read headers off the raw Request rather than the typed getRequestHeader
  // helper: its name parameter is a closed union from `fetchdts` that does not
  // include `sec-fetch-site`. That would be a type error which `bun run build`
  // hides, because esbuild strips types without checking them.
  const headers = getRequest().headers;

  // Fetch metadata is the clearest signal where the browser sends it.
  // "none" means user-initiated (typed URL, bookmark) — never a real POST here.
  const site = headers.get("sec-fetch-site");
  if (site && site !== "same-origin") {
    if (stateChanging || site === "cross-site") {
      throw new ForbiddenOriginError(`sec-fetch-site: ${site}`);
    }
  }

  const origin = headers.get("origin");
  if (origin) {
    if (!ALLOWED_ORIGINS.has(origin.replace(/\/+$/, "").toLowerCase())) {
      throw new ForbiddenOriginError(`origin: ${origin}`);
    }
    return;
  }

  // No Origin at all. Browsers always send it on cross-origin POSTs, so its
  // absence means same-origin GET or a non-browser client (curl). Reads may
  // proceed; anything that changes trading state may not.
  if (stateChanging) {
    throw new ForbiddenOriginError("no Origin header on a state-changing call");
  }
}

export const fetchHeartbeat = createServerFn({ method: "GET" }).handler(
  async () => {
    assertSameOrigin(false);
    return await _getHeartbeat();
  },
);

export const fetchOvernightRisk = createServerFn({ method: "GET" }).handler(
  async () => {
    assertSameOrigin(false);
    return await _getOvernightRisk();
  },
);

/**
 * Kill and reset are state-changing and deliberately POST-only, so a stray
 * GET — a crawler, a prefetch, a mistyped URL — can never halt trading.
 */
export const killSwitch = createServerFn({ method: "POST" }).handler(
  async () => {
    assertSameOrigin(true);
    return await _triggerKillSwitch();
  },
);

export const resetKill = createServerFn({ method: "POST" }).handler(
  async () => {
    assertSameOrigin(true);
    return await _resetKillSwitch();
  },
);
