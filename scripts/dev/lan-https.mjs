// HTTPS front for testing the HUD from a phone on the LAN (browsers only allow the mic on https).
//   https://<this-pc-ip>:3443  ->  next dev on 127.0.0.1:3001, and /v1/events  ->  hub on 127.0.0.1:8700
// Uses a self-signed certificate from .certs/ (see scripts/dev/README.md). Dev only; Ctrl+C stops it.
import { readFileSync } from "node:fs";
import http from "node:http";
import https from "node:https";
import net from "node:net";

const PORT = Number(process.env.LAN_HTTPS_PORT || 3443);
const UI = { host: "127.0.0.1", port: Number(process.env.UI_PORT || 3001) };
const HUB = { host: "127.0.0.1", port: Number(process.env.HUB_PORT || 8700) };
const certDir = new URL("../../.certs/", import.meta.url);
const tls = { key: readFileSync(new URL("lan.key", certDir)), cert: readFileSync(new URL("lan.crt", certDir)) };

const target = (url) => (url.startsWith("/v1/events") ? HUB : UI);

const server = https.createServer(tls, (req, res) => {
  const to = target(req.url);
  const up = http.request({ ...to, method: req.method, path: req.url,
    headers: { ...req.headers, "x-jarvis-proxy": "lan-https", "x-forwarded-proto": "https" } }, (r) => {
    res.writeHead(r.statusCode, r.headers);
    r.pipe(res);
  });
  up.on("error", () => { res.writeHead(502); res.end("upstream down"); });
  req.pipe(up);
});

// WebSockets (hub events, Next's hot reload): replay the handshake upstream, then splice the sockets.
server.on("upgrade", (req, sock, head) => {
  const up = net.connect(target(req.url), () => {
    const lines = [`${req.method} ${req.url} HTTP/1.1`];
    for (let i = 0; i < req.rawHeaders.length; i += 2) lines.push(`${req.rawHeaders[i]}: ${req.rawHeaders[i + 1]}`);
    up.write(lines.join("\r\n") + "\r\n\r\n");
    if (head?.length) up.write(head);
    sock.pipe(up).pipe(sock);
  });
  up.on("error", () => sock.destroy());
  sock.on("error", () => up.destroy());
});

server.listen(PORT, "0.0.0.0", () => console.log(`LAN HTTPS on :${PORT} -> ui :${UI.port}, events -> hub :${HUB.port}`));
