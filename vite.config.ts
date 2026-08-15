import tailwindcss from "@tailwindcss/vite";
import { tanstackStart } from "@tanstack/react-start/plugin/vite";
import viteReact from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import tsConfigPaths from "vite-tsconfig-paths";

// SECURITY — read before widening either of the two settings below.
//
// This dev server runs the server functions in src/server/actions.ts, which hold
// API_AUTH_TOKEN and call the bot on the caller's behalf. They have no
// authentication of their own. So whatever can reach this port can engage the
// kill switch, read the account, and place a manual entry.
//
//   host: true          -> binds 0.0.0.0, i.e. every machine on the network.
//   allowedHosts: true  -> disables Vite's Host-header check, which is what
//                          stops a page you merely *visit* from having your
//                          browser drive this server (DNS rebinding).
//
// Both were inherited from a reverse-proxied template where a sandbox sat in
// front. There is no proxy on a laptop, so both are restored to their safe
// defaults. Override via the DASHBOARD_* env vars only behind a firewall or an
// authenticating proxy.
const HOST = process.env.DASHBOARD_HOST ?? "127.0.0.1";
const PORT = Number(process.env.DASHBOARD_PORT ?? 3000);

if (HOST !== "127.0.0.1" && HOST !== "localhost") {
  console.warn(
    `WARNING: dev server binding to ${HOST}. This port can halt trading and ` +
      `read your account, and it has no login.`,
  );
}

export default defineConfig({
  server: {
    port: PORT,
    host: HOST,
    strictPort: true,
  },
  plugins: [
    tailwindcss(),
    tsConfigPaths({
      projects: ["./tsconfig.json"],
    }),
    tanstackStart(),
    viteReact(),
  ],
});
