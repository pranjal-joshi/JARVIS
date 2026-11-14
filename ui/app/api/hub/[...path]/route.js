// Server-side proxy to the hub. The web token stays on the server; the browser
// only ever talks to this UI. Only the paths the HUD needs are forwarded.
const HUB = process.env.HUB_INTERNAL_URL || "http://localhost:8700";
const ALLOWED = [
  /^chat\/completions$/,
  /^approvals(\/[\w-]+)?$/,
  /^audit$/,
  /^status$/,
  /^services$/,
  /^llm$/,
  /^kb\/(search|graph|memory|memory\/edit|tasks|sync|page|page\/(delete|rename)|block\/(delete|edit|add|move)|trash(\/restore)?)$/,
  /^traces(\/[\w.:-]+)?$/,
  /^audio\/(transcriptions|speech)$/,
  /^skills(\/.+\/(enable|run|promote))?$/,
  /^policy(\/grants\/[\w.*-]+)?$/,
  /^evals$/,
  /^dream(\/run|\/undo\/\d{1,3}|\/answer\/[0-9a-f]{8})?$/,
  /^wit$/,
  /^devices(\/pair(\/[a-f0-9]+)?|\/allow|\/disconnect)?$/,
  /^monitors$/,
  /^jobs\/([0-9a-f]{8}\/(dismiss|cancel)|clear)$/,
  /^watchers(\/test|\/draft|\/[a-z0-9-]+(\/(run|enable))?)?$/,
  /^notify\/test$/,
  /^webhooks(\/secret|\/test)?$/,
];
// Inbound webhooks from outside services: forwarded with the caller's own secret or signature and never
// with the web token, so a webhook can only start skills (POST only).
const HOOK = /^hooks\/[a-z0-9_-]{1,40}$/i;
const HOOK_HEADERS = ["authorization", "x-jarvis-token", "x-hub-signature-256", "x-github-event", "x-event-key",
  "x-gitlab-event", "x-jarvis-event", "content-type"];

export const dynamic = "force-dynamic";

async function forward(req, { params }) {
  const { path } = await params;
  const sub = path.join("/");
  const isHook = HOOK.test(sub);
  if (isHook && req.method !== "POST") return Response.json({ detail: "POST only" }, { status: 405 });
  if (!isHook && !ALLOWED.some((re) => re.test(sub))) {
    return Response.json({ detail: "not allowed" }, { status: 404 });
  }
  const url = new URL(req.url);
  const headers = {};
  if (isHook) {
    for (const h of HOOK_HEADERS) {
      const v = req.headers.get(h);
      if (v) headers[h] = v;
    }
  } else {
    headers.Authorization = `Bearer ${process.env.HUB_TOKEN_WEB || ""}`;
    const ct = req.headers.get("content-type");
    if (ct) headers["Content-Type"] = ct;
  }
  const session = isHook ? null : req.headers.get("x-jarvis-session"); // the HUD's conversation id, for the hub's traces
  if (session) headers["X-Jarvis-Session"] = session;

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

export { forward as GET, forward as POST, forward as DELETE };
