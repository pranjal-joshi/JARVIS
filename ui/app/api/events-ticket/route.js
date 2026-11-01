// Hands the browser a one-time ticket for the hub's event WebSocket.
const HUB = process.env.HUB_INTERNAL_URL || "http://localhost:8700";

export const dynamic = "force-dynamic";

// Behind the LAN HTTPS proxy (scripts/dev/lan-https.mjs) the socket goes through the proxy too,
// so an https page never needs a plain ws:// connection (browsers block that as mixed content).
const viaProxy = (req) => (req.headers.get("x-jarvis-proxy") === "lan-https" ? req.headers.get("host") : null);

export async function GET(req) {
  try {
    const res = await fetch(`${HUB}/v1/events/ticket`, {
      method: "POST",
      headers: { Authorization: `Bearer ${process.env.HUB_TOKEN_WEB || ""}` },
      cache: "no-store",
    });
    if (!res.ok) return Response.json({ detail: "ticket refused" }, { status: res.status });
    const { ticket } = await res.json();
    const host = viaProxy(req);
    const base = host ? `wss://${host}` : process.env.HUB_PUBLIC_WS_URL || "ws://localhost:8700";
    return Response.json({ ticket, wsUrl: `${base}/v1/events` });
  } catch {
    return Response.json({ detail: "hub unreachable" }, { status: 502 });
  }
}
