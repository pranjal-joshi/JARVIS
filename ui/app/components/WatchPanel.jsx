"use client";

// Watchers: look-only checks JARVIS runs on a schedule (new PRs ready to merge, a pipeline that finished,
// new mail), notifying you of items it hasn't seen before. Only you add or change them, here; JARVIS has
// no tool for it. Describe one in plain words and "Draft with Claude" fills the form (that button is the
// only thing that asks Claude), then Test runs it once and shows what it found, before you save.

import { useCallback, useEffect, useState } from "react";

const api = (path, init) => fetch(`/api/hub/${path}`, { cache: "no-store", ...init });
const post = (path, body, method = "POST") => api(path, {
  method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined,
});
const TARGETS = [["windows", "laptop"], ["android", "phone"], ["hud", "HUD"], ["voice", "voice"]];
const BLANK = { name: "", description: "", every: "5m", shell: "pwsh", command: "", key: "id", title: "", text: "",
  url: "", notify: ["windows", "hud"], env: [], enabled: true };
const why = async (r) => { try { return (await r.json()).detail || r.statusText; } catch { return r.statusText; } };

export default function WatchPanel({ data, onChange }) {
  const [edit, setEdit] = useState(null); // { form, replaces }
  const [ask, setAsk] = useState("");
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");
  const [test, setTest] = useState(null);
  const [trust, setTrust] = useState(false);
  const watchers = data?.watchers || [];

  const done = useCallback(() => { onChange?.(); }, [onChange]);
  useEffect(() => { setTest(null); setTrust(false); }, [edit?.replaces]);

  const act = async (label, fn) => {
    setBusy(label); setMsg("");
    try { await fn(); } catch (e) { setMsg(String(e.message || e)); } finally { setBusy(""); }
  };
  const field = (k) => ({ value: Array.isArray(edit.form[k]) ? edit.form[k].join(", ") : edit.form[k] ?? "",
    onChange: (e) => setEdit({ ...edit, form: { ...edit.form, [k]: k === "env"
      ? e.target.value.split(/[,\s]+/).filter(Boolean) : e.target.value } }) });

  const draft = () => act("draft", async () => {
    const r = await post("watchers/draft", { description: ask, current: edit?.form?.command ? edit.form : null });
    if (!r.ok) throw new Error(await why(r));
    const d = await r.json();
    setEdit({ form: { ...BLANK, ...d.draft }, replaces: edit?.replaces || "" });
    setTest(d.test ? d.test : null);
    setMsg(d.test_error ? `Claude's draft didn't pass the test: ${d.test_error}` : `Drafted by ${d.model || "Claude"} in ${d.seconds ?? "?"} s; check it, then save.`);
  });
  const runTest = () => act("test", async () => {
    const r = await post("watchers/test", { watcher: edit.form });
    if (!r.ok) throw new Error(await why(r));
    setTest(await r.json());
  });
  const save = () => act("save", async () => {
    const r = await post("watchers", { watcher: edit.form, accept_unverified: trust, replaces: edit.replaces });
    if (!r.ok) throw new Error(await why(r));
    setEdit(null); setAsk(""); setMsg("Saved. Its first run only learns what's there; you'll hear about new items after that.");
    done();
  });
  const notifyTest = (to) => act(`n-${to}`, async () => {
    const r = await post("notify/test", { title: "JARVIS", text: "Test notification, sir.", to: [to] });
    const d = await r.json();
    setMsg(`${to}: ${d[to] || "?"}`);
  });

  return (
    <div className="dev-panel watch-panel">
      <p className="tools-note">
        <b>Watchers</b> run a look-only check every few minutes and tell you about new items: PRs ready to merge, a
        pipeline that finished, new mail. Describe what you want and let Claude draft it, or write the command
        yourself. Commands that would change files are refused.
      </p>
      <div className="watch-notify">
        test a notification:
        {TARGETS.map(([t, label]) => (
          <button key={t} type="button" className="gate" disabled={!!busy} onClick={() => notifyTest(t)}>{label}</button>
        ))}
      </div>
      {msg && <p className="watch-msg">{msg}</p>}

      {!edit && (
        <>
          <ul className="skill-list">
            {watchers.map((w) => (
              <li key={w.name}>
                <div className="skill-head">
                  <b>{w.title || w.name}</b>
                  <button type="button" className={`gate ${w.enabled ? "auto" : "ask"}`} disabled={!!busy}
                    onClick={() => act("toggle", async () => {
                      const r = await post(`watchers/${w.name}/enable`, { enabled: !w.enabled });
                      if (!r.ok) throw new Error(await why(r)); done();
                    })}>{w.enabled ? "on" : "paused"}</button>
                  <button type="button" className="gate" disabled={!!busy}
                    onClick={() => act("run", async () => {
                      const r = await post(`watchers/${w.name}/run`);
                      if (!r.ok) throw new Error(await why(r));
                      const d = await r.json();
                      setMsg(`${w.name}: ${d.items?.length ?? 0} items, ${d.learned ? "learned (first run)" : `${d.new?.length ?? 0} new`}`); done();
                    })}>run</button>
                  <button type="button" className="gate" disabled={!!busy}
                    onClick={() => setEdit({ form: { ...BLANK, ...w }, replaces: w.name })}>edit</button>
                  <button type="button" className="gate deny" disabled={!!busy}
                    onClick={() => confirm(`Delete the watcher "${w.name}"?`) && act("del", async () => {
                      const r = await post(`watchers/${w.name}`, null, "DELETE");
                      if (!r.ok) throw new Error(await why(r)); done();
                    })}>✕</button>
                </div>
                <i>every {w.every} · {w.name}{w.verified ? " · look-only" : " · trusted by you"}
                  {w.last_error ? ` · error: ${w.last_error}` : ""}</i>
              </li>
            ))}
            {!watchers.length && <li className="empty">No watchers yet.</li>}
          </ul>
          <button type="button" className="gate watch-new" onClick={() => setEdit({ form: { ...BLANK }, replaces: "" })}>＋ new watcher</button>
        </>
      )}

      {edit && (
        <div className="watch-edit">
          <label className="wide">Describe it
            <textarea rows={2} value={ask} onChange={(e) => setAsk(e.target.value)}
              placeholder="e.g. tell me on my laptop when a PR I'm reviewing in org/repo is approved and checks pass" />
          </label>
          <button type="button" className="gate expert" disabled={!ask.trim() || !!busy} onClick={draft}
            title="Sends your description (and the current form) to Claude through the expert bridge">
            {busy === "draft" ? "Claude is drafting…" : "⟁ Draft with Claude"}</button>
          <label>name<input {...field("name")} placeholder="prs-ready" /></label>
          <label>every<input {...field("every")} placeholder="5m" /></label>
          <label className="wide">what it watches<input {...field("description")} /></label>
          <label className="wide">command (prints JSON)<textarea rows={3} {...field("command")} spellCheck={false} /></label>
          <label>shell<select {...field("shell")}><option value="pwsh">PowerShell 7</option>
            <option value="powershell">Windows PowerShell 5.1</option></select></label>
          <label>item key<input {...field("key")} placeholder="url" /></label>
          <label>title<input {...field("title")} /></label>
          <label>text<input {...field("text")} placeholder="#{number} {title}" /></label>
          <label>link<input {...field("url")} placeholder="{url}" /></label>
          <label>env vars<input {...field("env")} placeholder="ADO_PAT" /></label>
          <div className="wide watch-targets">notify on
            {TARGETS.map(([t, label]) => (
              <label key={t} className="chk"><input type="checkbox" checked={edit.form.notify.includes(t)}
                onChange={(e) => setEdit({ ...edit, form: { ...edit.form, notify: e.target.checked
                  ? [...edit.form.notify, t] : edit.form.notify.filter((x) => x !== t) } })} /> {label}</label>
            ))}
          </div>
          {test && (
            <div className="wide watch-test">
              <b>{test.items?.length ?? 0} items now</b>
              {test.watcher && !test.watcher.verified && (
                <p className="warn">⚠ Not proven look-only ({test.watcher.why}). Save it only if you trust the command.
                  <label className="chk"><input type="checkbox" checked={trust} onChange={(e) => setTrust(e.target.checked)} /> I trust it</label></p>
              )}
              <ul>{(test.items || []).slice(0, 5).map((i) => <li key={i.key}>{i.text || i.key}</li>)}</ul>
            </div>
          )}
          <div className="wide watch-actions">
            <button type="button" className="gate" disabled={!edit.form.command || !!busy} onClick={runTest}>
              {busy === "test" ? "testing…" : "test"}</button>
            <button type="button" className="gate auto" disabled={!test || !!busy || (!test.watcher?.verified && !trust)}
              onClick={save} title={test ? "" : "Test it first"}>save</button>
            <button type="button" className="gate" onClick={() => { setEdit(null); setMsg(""); }}>cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}
