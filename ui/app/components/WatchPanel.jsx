"use client";

// Watchers: look-only checks JARVIS runs on a schedule (new PRs ready to merge, a pipeline that finished,
// new mail), notifying you of items it hasn't seen before. Only you add or change them, here; JARVIS has
// no tool for it. You only say what you want: Claude writes the check (that button is the only thing that
// asks Claude), may ask you one thing it can't find out, and fixes its own draft once if the test fails.
// The draft is run once and shown before you save; the details stay editable under "Edit details".

import { useState } from "react";

const api = (path, init) => fetch(`/api/hub/${path}`, { cache: "no-store", ...init });
const post = (path, body, method = "POST") => api(path, {
  method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined,
});
const TARGETS = [["windows", "laptop"], ["android", "phone"], ["hud", "HUD"], ["voice", "voice"]];
const BLANK = { name: "", description: "", every: "5m", shell: "pwsh", command: "", key: "id", title: "", text: "",
  url: "", notify: ["windows", "hud"], env: [], enabled: true };
const why = async (r) => { try { return (await r.json()).detail || r.statusText; } catch { return r.statusText; } };

export default function WatchPanel({ data, onChange }) {
  const [edit, setEdit] = useState(null); // { form, replaces, details }
  const [ask, setAsk] = useState(""); // what you want, in your words
  const [qa, setQa] = useState([]); // Claude's questions and your answers
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");
  const [test, setTest] = useState(null);
  const [trust, setTrust] = useState(false);
  const watchers = data?.watchers || [];
  const pending = qa.length && !qa[qa.length - 1].answer ? qa[qa.length - 1] : null;

  const reset = () => { setEdit(null); setAsk(""); setQa([]); setReply(""); setTest(null); setTrust(false); };
  const act = async (label, fn) => {
    setBusy(label); setMsg("");
    try { await fn(); } catch (e) { setMsg(String(e.message || e)); } finally { setBusy(""); }
  };
  const set = (patch) => setEdit((e) => ({ ...e, form: { ...e.form, ...patch } }));
  const field = (k) => ({ value: Array.isArray(edit.form[k]) ? edit.form[k].join(", ") : edit.form[k] ?? "",
    onChange: (e) => set({ [k]: k === "env" ? e.target.value.split(/[,\s]+/).filter(Boolean) : e.target.value }) });

  // Claude drafts (or changes) the watcher from your words; answers carries the questions it asked so far
  const draft = (answers) => act("draft", async () => {
    const r = await post("watchers/draft", { description: ask, answers,
      current: edit?.replaces || edit?.form?.command ? edit.form : null });
    if (!r.ok) throw new Error(await why(r));
    const d = await r.json();
    if (d.question) { setQa([...answers, { question: d.question, answer: "" }]); return; }
    setQa(answers);
    const notify = edit?.form?.notify?.length ? edit.form.notify : undefined; // keep your choice of targets
    setEdit((e) => ({ form: { ...BLANK, ...d.draft, ...(notify ? { notify } : {}) }, replaces: e?.replaces || "",
      details: e?.details || false }));
    setTest(d.test || null); setTrust(false);
    setMsg(d.test_error ? `It didn't pass its test: ${d.test_error}. Say what to change, or edit the details.`
      : `Ready${d.fixed ? " (Claude fixed its first try)" : ""}, ${d.seconds ?? "?"} s. Check it and save.`);
  });
  const answer = () => {
    const answers = qa.map((x, i) => (i === qa.length - 1 ? { ...x, answer: reply } : x));
    setReply(""); draft(answers);
  };
  const runTest = () => act("test", async () => {
    const r = await post("watchers/test", { watcher: edit.form });
    if (!r.ok) throw new Error(await why(r));
    setTest(await r.json()); setTrust(false);
  });
  const save = () => act("save", async () => {
    const r = await post("watchers", { watcher: edit.form, accept_unverified: trust, replaces: edit.replaces });
    if (!r.ok) throw new Error(await why(r));
    reset();
    setMsg("Saved. Its first run only learns what's there; you'll hear about new items after that.");
    onChange?.();
  });
  const notifyTest = (to) => act(`n-${to}`, async () => {
    const r = await post("notify/test", { title: "JARVIS", text: "Test notification, sir.", to: [to] });
    const d = await r.json();
    setMsg(`${to}: ${d[to] || "?"}`);
  });
  const toggleTarget = (t, on) => set({ notify: on ? [...edit.form.notify, t] : edit.form.notify.filter((x) => x !== t) });
  const creating = edit && !edit.form.command; // nothing drafted yet

  return (
    <div className="dev-panel watch-panel">
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
                      if (!r.ok) throw new Error(await why(r)); onChange?.();
                    })}>{w.enabled ? "on" : "paused"}</button>
                  <button type="button" className="gate" disabled={!!busy}
                    onClick={() => act("run", async () => {
                      const r = await post(`watchers/${w.name}/run`);
                      if (!r.ok) throw new Error(await why(r));
                      const d = await r.json();
                      setMsg(`${w.name}: ${d.items?.length ?? 0} items, ${d.learned ? "learned (first run)" : `${d.new?.length ?? 0} new`}`);
                      onChange?.();
                    })}>run</button>
                  <button type="button" className="gate" disabled={!!busy}
                    onClick={() => { reset(); setEdit({ form: { ...BLANK, ...w }, replaces: w.name, details: false }); }}>change</button>
                  <button type="button" className="gate deny" disabled={!!busy}
                    onClick={() => confirm(`Delete the watcher "${w.name}"?`) && act("del", async () => {
                      const r = await post(`watchers/${w.name}`, null, "DELETE");
                      if (!r.ok) throw new Error(await why(r)); onChange?.();
                    })}>✕</button>
                </div>
                <i>{w.description || w.name} · every {w.every}{w.last_error ? ` · error: ${w.last_error}` : ""}</i>
              </li>
            ))}
            {!watchers.length && <li className="empty">Nothing watched yet.</li>}
          </ul>
          <button type="button" className="gate watch-new" onClick={() => { reset(); setEdit({ form: { ...BLANK }, replaces: "", details: false }); }}>
            ＋ watch something</button>
        </>
      )}

      {edit && (
        <div className="watch-edit">
          {/* 1. what you want: the only thing you have to write */}
          <label className="wide">{edit.replaces ? `What should change in "${edit.form.title || edit.replaces}"?` : "What should I watch for?"}
            <textarea rows={3} value={ask} onChange={(e) => setAsk(e.target.value)} autoFocus
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && ask.trim() && !busy) { e.preventDefault(); draft([]); } }}
              placeholder={edit.replaces ? "e.g. check every 15 minutes and tell my phone too"
                : "e.g. tell me when a PR I'm reviewing in org/repo is approved and its checks pass"} />
          </label>
          {!pending && (
            <button type="button" className="gate expert" disabled={!ask.trim() || !!busy} onClick={() => draft([])}
              title="Claude writes the check from your words (sent through the expert bridge); you see it before saving">
              {busy === "draft" ? "Claude is setting it up…" : edit.replaces ? "⟁ Change it" : "⟁ Set it up"}</button>
          )}

          {/* 2. Claude asks only what it can't find out itself */}
          {qa.filter((x) => x.answer).map((x, i) => (
            <p key={i} className="wide watch-qa"><b>Claude:</b> {x.question}<br /><b>You:</b> {x.answer}</p>
          ))}
          {pending && (
            <div className="wide watch-ask">
              <p><b>Claude asks:</b> {pending.question}</p>
              <div className="watch-actions">
                <input value={reply} onChange={(e) => setReply(e.target.value)} autoFocus placeholder="your answer"
                  onKeyDown={(e) => { if (e.key === "Enter" && reply.trim()) answer(); }} />
                <button type="button" className="gate expert" disabled={!reply.trim() || !!busy} onClick={answer}>
                  {busy === "draft" ? "…" : "answer"}</button>
              </div>
            </div>
          )}

          {/* 3. the result in plain words: what, how often, where you'll hear about it, what it found */}
          {!creating && (
            <div className="wide watch-summary">
              <b>{edit.form.title || edit.form.name}</b>
              <span>{edit.form.description}</span>
              <span>checks every {edit.form.every}{test ? ` · finds ${test.items?.length ?? 0} item${test.items?.length === 1 ? "" : "s"} right now` : ""}</span>
              <div className="watch-targets">tell me on
                {TARGETS.map(([t, label]) => (
                  <label key={t} className="chk"><input type="checkbox" checked={edit.form.notify.includes(t)}
                    onChange={(e) => toggleTarget(t, e.target.checked)} /> {label}</label>
                ))}
              </div>
              {test?.items?.length > 0 && (
                <ul>{test.items.slice(0, 4).map((i) => <li key={i.key}>{i.text || i.key}</li>)}</ul>
              )}
              {test?.watcher && !test.watcher.verified && (
                <p className="warn">⚠ Its command isn't proven look-only ({test.watcher.why}). Save only if you trust it.
                  <label className="chk"><input type="checkbox" checked={trust} onChange={(e) => setTrust(e.target.checked)} /> I trust it</label></p>
              )}
            </div>
          )}

          {/* 4. optional: everything Claude filled in, editable */}
          {!creating && (
            <button type="button" className="gate wide-left" onClick={() => setEdit({ ...edit, details: !edit.details })}
              aria-expanded={edit.details}>{edit.details ? "▾ hide details" : "▸ edit details"}</button>
          )}
          {!creating && edit.details && (
            <>
              <label>name<input {...field("name")} /></label>
              <label>every<input {...field("every")} placeholder="5m" /></label>
              <label className="wide">description<input {...field("description")} /></label>
              <label className="wide">command (prints JSON)<textarea rows={3} {...field("command")} spellCheck={false} /></label>
              <label>shell<select {...field("shell")}><option value="pwsh">PowerShell 7</option>
                <option value="powershell">Windows PowerShell 5.1</option></select></label>
              <label>item key<input {...field("key")} /></label>
              <label>title<input {...field("title")} /></label>
              <label>text<input {...field("text")} /></label>
              <label>link<input {...field("url")} /></label>
              <label>env vars<input {...field("env")} placeholder="ADO_PAT" /></label>
            </>
          )}

          <div className="wide watch-actions">
            {!creating && <button type="button" className="gate" disabled={!!busy} onClick={runTest}>
              {busy === "test" ? "testing…" : "test again"}</button>}
            {!creating && <button type="button" className="gate auto" disabled={!test || !!busy || (!test.watcher?.verified && !trust)}
              onClick={save} title={test ? "" : "Test it first"}>save</button>}
            <button type="button" className="gate" onClick={() => { reset(); setMsg(""); }}>cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}
