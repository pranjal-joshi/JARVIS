// Server-side proxy to the hub. The web token stays on the server; the browser
// only ever talks to this UI. Only the paths the HUD needs are forwarded.
const HUB = process.env.HUB_INTERNAL_URL || "http://localhost:8700";
const ALLOWED = [
  /^chat\/completions$/,
  /^approvals(\/[\w-]+)?$/,
  /^audit$/,
  /^status$/,
  /^llm$/,
  /^kb\/(search|graph|memory|tasks|sync|daily-summary)$/,
  /^audio\/(transcriptions|speech)$/,
];

export const dynamic = "force-dynamic";

async function forward(req, { params }) {
  const { path } = await params;
  const sub = path.join("/");
  if (!ALLOWED.some((re) => re.test(sub))) {
    return Response.json({ detail: "not allowed" }, { status: 404 });
  }
  const url = new URL(req.url);
  const headers = { Authorization: `Bearer ${process.env.HUB_TOKEN_WEB || ""}` };
  const ct = req.headers.get("content-type");
  if (ct) headers["Content-Type"] = ct;

  const init = { method: req.method, headers, cache: "no-store" };
  if (!["GET", "HEAD"].includes(req.method)) init.body = await req.arrayBuffer();

  try {
    const res = await fetch(`${HUB}/v1/${sub}${url.search}`, init);
    const out = new Headers();
    for (const h of ["content-type", "x-tts-engine"]) {
      const v = res.headers.get(h);
      if (v) out.set(h, v);
    }
    return new Response(res.body, { status: res.status, headers: out });
  } catch {
    return Response.json({ detail: "hub unreachable" }, { status: 502 });
  }
}

export { forward as GET, forward as POST };
