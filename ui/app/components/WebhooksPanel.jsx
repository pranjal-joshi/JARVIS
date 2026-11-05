"use client";

// Webhooks: outside services (Home Assistant, GitHub, Azure DevOps, IFTTT, a script) POST to a hook and the
// skills with `trigger:: webhook` and `match:: <hook name>` run. Here: turn it on (make the secret), rotate or
// turn it off, copy a ready curl, fire a test, and see the last deliveries (accepted or refused).

import { useCallback, useEffect, useState } from "react";

const api = (path, init) => fetch(`/api/hub/${path}`, { cache: "no-store", ...init });
const post = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});
const time = (t) => new Date(t * 1000).toLocaleTimeString();
const STATUS = { 202: "ok", 401: "bad secret", 404: "off", 413: "too big", 429: "rate limited" };

export default function WebhooksPanel({ version }) {
  const [data, setData] = useState(null);
  const [secret, setSecret] = useState(null); // shown after make/rotate/reveal, never stored
  const [name, setName] = useState("test");
  const [body, setBody] = useState('{"message": "hello from a webhook"}');
  const [note, setNote] = useState("");
  const [copied, setCopied] = useState("");

  const load = useCallback(async () => {
    const r = await api("webhooks").catch(() => null);
    if (r?.ok) setData(await r.json());
  }, []);
  useEffect(() => { load(); }, [load, version]);

  const origin = typeof window !== "undefined" ? window.location.origin : "";
  const hook = (name || "test").toLowerCase().replace(/[^a-z0-9_-]/g, "-").slice(0, 40);
  const url = `${origin}/api/hub/hooks/${hook}`;
  const curl = `curl -X POST "${url}" -H "X-Jarvis-Token: ${secret || "<secret>"}" `
    + `-H "Content-Type: application/json" -d '${body.replace(/'/g, "'\\''")}'`;
  // trigger and match spelled out: a small model otherwise writes "run on webhook X" into the steps
  const example = `Make a skill called ${hook} alert with trigger webhook and match ${hook}. `
    + `Its steps: tell me in one short sentence what the webhook says. `
    + `Then send me a notification with that sentence.`;
  // PowerShell: single-quoted strings are literal ('' is a quote), so the JSON body and secret pass untouched
  const psq = (v) => `'${String(v).replace(/'/g, "''")}'`;
  const ps = `Invoke-RestMethod -Method Post -Uri ${psq(url)} -Headers @{ 'X-Jarvis-Token' = ${psq(secret || "<secret>")} } `
    + `-ContentType 'application/json' -Body ${psq(body)}`;

  const copy = async (what, text) => {
    try { await navigator.clipboard.writeText(text); setCopied(what); setTimeout(() => setCopied(""), 1500); }
    catch { setNote("copy failed: select the text by hand"); }
  };

  const secretAction = async (action) => {
    if (action === "rotate" && data?.mode === "hud"
        && !confirm("Make a new secret? Services using the old one stop working.")) return;
    if (action === "disable" && !confirm("Turn webhooks off? Every call is refused until you make a new secret.")) return;
    const r = await post("webhooks/secret", { action });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) setNote(d.detail || "failed");
    else { setSecret(d.secret || null); setNote(action === "rotate" ? "New secret: copy it now; it stays hidden after you close this." : "Webhooks are off."); }
    load();
  };

  const reveal = async () => {
    const r = await api("webhooks?reveal=true");
    if (r.ok) setSecret((await r.json()).secret);
  };

  const test = async () => {
    setNote("");
    const r = await post("webhooks/test", { name: hook, body });
    const d = await r.json().catch(() => ({}));
    setNote(!r.ok ? (d.detail || "failed")
      : d.skills?.length ? `Fired: ${d.skills.map((s) => s.replace(/^skills\//i, "")).join(", ")}`
        : `Delivered as “${d.text}”, but no enabled skill has trigger:: webhook and match:: ${hook}.`);
    load();
  };

  if (!data) return <div className="dev-panel"><p className="tools-note">Loading…</p></div>;
  const on = data.mode !== "off";

  return (
    <div className="dev-panel hooks-panel">
      <p className="tools-note">
        A POST to a hook runs the skills with <b>trigger:: webhook</b> and <b>match::</b> the hook name. The body is
        treated as data, never as instructions; writes and sends still ask you first.
      </p>

      <div className="hooks-row">
        <span className={`gate ${on ? "auto" : "ask"}`}>{on ? "on" : "off"}</span>
        <span className="hooks-secret">
          {data.mode === "env" ? <>secret from <b>HUB_WEBHOOK_SECRET</b> {data.hint}</>
            : on ? <>secret {secret ? <code>{secret}</code> : data.hint}</> : "no secret yet"}
        </span>
        {on && !secret && <button type="button" className="gate" onClick={reveal}>show</button>}
        {secret && <button type="button" className="gate" onClick={() => copy("secret", secret)}>{copied === "secret" ? "copied" : "copy"}</button>}
        {data.mode !== "env" && (
          <button type="button" className="gate" onClick={() => secretAction("rotate")}>{on ? "rotate" : "turn on"}</button>
        )}
        {data.mode === "hud" && <button type="button" className="gate" onClick={() => secretAction("disable")}>turn off</button>}
      </div>

      <div className="hooks-try">
        <label>hook <input value={name} onChange={(e) => setName(e.target.value)} list="hook-names" spellCheck={false} /></label>
        <datalist id="hook-names">{data.hooks.map((h) => <option key={h} value={h} />)}</datalist>
        <label>body <textarea rows={2} value={body} onChange={(e) => setBody(e.target.value)} spellCheck={false} /></label>
        <div className="hooks-url"><code>POST {url}</code></div>
        <div className="hooks-actions">
          <button type="button" className="gate" onClick={test} title="Runs the matching skills now, without the secret">fire test</button>
          <button type="button" className="gate" disabled={!on} onClick={() => copy("curl", curl)}>
            {copied === "curl" ? "copied" : "copy curl"}
          </button>
          <button type="button" className="gate" disabled={!on} onClick={() => copy("ps", ps)}
            title="Invoke-RestMethod for Windows PowerShell / pwsh (curl's quoting breaks there)">
            {copied === "ps" ? "copied" : "copy PowerShell"}
          </button>
          <button type="button" className="gate" onClick={() => copy("url", url)}>{copied === "url" ? "copied" : "copy url"}</button>
        </div>
        {note && <p className="tools-note hooks-note">{note}</p>}
      </div>

      <div className="hook-log">
        <h4>Skills listening</h4>
        {data.skills.length ? data.skills.map((s) => (
          <div key={s.name}>
            <span className={s.enabled ? "" : "off"}>{s.enabled ? "●" : "○"}</span> {s.name.replace(/^skills\//i, "")}
            {" · "}{s.match ? `hook “${s.match}”` : "every hook"}{s.last_run ? ` · ran ${time(s.last_run)}` : ""}
          </div>
        )) : <div>None yet for any hook.</div>}
      </div>

      <div className="hook-log">
        <h4>Example skill for “{hook}”</h4>
        <div>Say or paste this to JARVIS, then <b>fire test</b>:</div>
        <code className="hooks-example">{example}</code>
        <div className="hooks-actions">
          <button type="button" className="gate" onClick={() => copy("example", example)}>
            {copied === "example" ? "copied" : "copy prompt"}
          </button>
        </div>
      </div>

      <div className="hook-log">
        <h4>Last deliveries</h4>
        {data.log.length ? data.log.slice(0, 10).map((e, i) => (
          <div key={i} className={e.status === 202 ? "" : "bad"}>
            <span>{time(e.ts)}</span> {e.hook} · {STATUS[e.status] || e.status}{e.test ? " (test)" : ""}
            {e.skills?.length ? ` → ${e.skills.map((s) => s.replace(/^skills\//i, "")).join(", ")}` : ""}
            {e.note && e.status === 202 ? <small> · {e.note}</small> : null}
          </div>
        )) : <div>Nothing yet.</div>}
      </div>

      <p className="tools-note">
        From another container on the stack: <code>http://hub:8700/v1/hooks/&lt;name&gt;</code>. From the internet
        (GitHub), put a tunnel or reverse proxy in front of this HUD; GitHub can sign with the secret instead
        (X-Hub-Signature-256). Limits: {data.limits.per_hook_per_min}/min per hook, {data.limits.per_min}/min in all,
        {" "}{Math.round(data.limits.max_body / 1024)} KB body.
      </p>
    </div>
  );
}
