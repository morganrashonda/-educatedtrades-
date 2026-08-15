// Production server for the dashboard. The TanStack Start build emits a portable
// fetch handler (dist/server/server.js) plus static client assets (dist/client);
// this wraps them in a Bun server — static files first, SSR for the rest.
// Run `bun run build` before starting.
//
// SECURITY: this server holds API_AUTH_TOKEN and calls the bot on the operator's
// behalf. The server functions in src/server/actions.ts have no authentication of
// their own — they cannot, because the browser has no credential to present. So
// anything that can reach this port can engage the kill switch and read the
// account. The bot's own API is bound to loopback precisely to prevent that;
// binding the dashboard wider punches a hole straight through it.
//
// Therefore: loopback by default. DASHBOARD_HOST can widen it, but only
// deliberately, and only behind a firewall or an authenticating reverse proxy.
import handler from "./dist/server/server.js";

const PORT = Number(process.env.DASHBOARD_PORT ?? 3000);
const HOST = process.env.DASHBOARD_HOST ?? "127.0.0.1";
const CLIENT_DIR = `${import.meta.dir}/dist/client`;

if (HOST !== "127.0.0.1" && HOST !== "localhost") {
  console.warn(
    `WARNING: dashboard binding to ${HOST}, not loopback. This port can halt ` +
      `trading and read your account, and it has no login. Only do this behind ` +
      `a firewall or an authenticating proxy.`,
  );
}

// Bun.serve throws EADDRINUSE synchronously. Report it as the operator error it
// is rather than killing whoever holds the port: on a personal machine the
// process on :3000 is more likely to be something else of the user's than a
// stale copy of this server, and killing it silently is not ours to do.
try {
  Bun.serve({
    port: PORT,
    hostname: HOST,
    async fetch(req) {
      const { pathname } = new URL(req.url);
      if (pathname !== "/") {
        const file = Bun.file(CLIENT_DIR + pathname);
        if (await file.exists()) return new Response(file);
      }
      return (
        handler as { fetch: (r: Request) => Response | Promise<Response> }
      ).fetch(req);
    },
  });
} catch (err) {
  const code = (err as { code?: string }).code;
  if (code === "EADDRINUSE") {
    console.error(
      `Port ${String(PORT)} is already in use.\n` +
        `  Find it:  lsof -iTCP:${String(PORT)} -sTCP:LISTEN\n` +
        `  Or pick another:  DASHBOARD_PORT=3001 bun run start`,
    );
    process.exit(1);
  }
  throw err;
}

console.log(`dashboard serving on http://${HOST}:${String(PORT)}`);
