// Production HUD on the host, reachable from the LAN: http://<this-pc>:3001, or https://<this-pc>:3443
// through scripts/dev/lan-https.mjs (browsers only allow the mic on https).
//   node scripts/dev/serve-ui.mjs             build, then serve
//   node scripts/dev/serve-ui.mjs --no-build  serve the last build
// Runs the standalone server that `output: "standalone"` builds (what the Docker UI image runs too).
// Needs the hub on localhost:8700. Reads HUB_TOKEN_WEB from .env (never printed). Ctrl+C stops it.
import { execSync, spawn } from "node:child_process";
import { cpSync, existsSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../../", import.meta.url));
const ui = join(root, "ui");
const env = Object.fromEntries(readFileSync(join(root, ".env"), "utf8").split(/\r?\n/)
  .filter((l) => /^[A-Z_]+=/.test(l)).map((l) => [l.slice(0, l.indexOf("=")), l.slice(l.indexOf("=") + 1)]));

if (!process.argv.includes("--no-build")) execSync("npx next build", { cwd: ui, stdio: "inherit" });

// the standalone server doesn't include static assets or public/: copy them next to it
const out = join(ui, ".next", "standalone");
rmSync(join(out, ".next", "static"), { recursive: true, force: true });
cpSync(join(ui, ".next", "static"), join(out, ".next", "static"), { recursive: true });
if (existsSync(join(ui, "public"))) {
  rmSync(join(out, "public"), { recursive: true, force: true });
  cpSync(join(ui, "public"), join(out, "public"), { recursive: true });
}

const server = spawn(process.execPath, [join(out, "server.js")], {
  cwd: out, stdio: "inherit",
  env: {
    ...process.env,
    HUB_TOKEN_WEB: env.HUB_TOKEN_WEB || "",
    HUB_INTERNAL_URL: process.env.HUB_INTERNAL_URL || "http://localhost:8700",
    HUB_PUBLIC_WS_URL: process.env.HUB_PUBLIC_WS_URL || "ws://localhost:8700",
    PORT: process.env.UI_PORT || "3001",
    HOSTNAME: "0.0.0.0",
  },
});
for (const sig of ["SIGINT", "SIGTERM"]) process.on(sig, () => server.kill(sig));
server.on("exit", (code) => process.exit(code ?? 0));
