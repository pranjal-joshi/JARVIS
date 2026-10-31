// Hands the browser a one-time ticket for the hub's event WebSocket.
const HUB = process.env.HUB_INTERNAL_URL || "http://localhost:8700";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const res = await fetch(`${HUB}/v1/events/ticket`, {
      method: "POST",
      headers: { Authorization: `Bearer ${process.env.HUB_TOKEN_WEB || ""}` },
      cache: "no-store",
    });
    if (!res.ok) return Response.json({ detail: "ticket refused" }, { status: res.status });
    const { ticket } = await res.json();
    return Response.json({
      ticket,
      wsUrl: `${process.env.HUB_PUBLIC_WS_URL || "ws://localhost:8700"}/v1/events`,
    });
  } catch {
    return Response.json({ detail: "hub unreachable" }, { status: 502 });
  }
}
