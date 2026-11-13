"use client";

// The parts of the HUD that reshape themselves around what is happening, all drawn from the event stream:
//   Widget        a fixed kit (list, compare, stats, progress, forecast, countdown) the hub fills with data.
//                 The model never sends markup; every value is shown as text (tools/widget.py).
//   Constellation the subagents at work (jobs, Claude, web lookups, skills, a bigger model) orbiting the orb.
//   LiveNote      what JARVIS is writing to your notes, line by line, with the pages it links.
//   Toasts        pop-ups that fade by importance (approvals stay) and a "while you were away" count.
//   DreamDigest   what last night's dream changed on the memory pages, each with undo, and its questions.
//   Preview       an approval's effect: a note diff or the devices it touches.
//   WitPanel      the wit dial, for the house and each room.
// Events reach them through a feed (Hud.jsx): a Set of listeners the one WebSocket calls.

import { useCallback, useEffect, useRef, useState } from "react";

const api = (path, init) => fetch(`/api/hub/${path}`, { cache: "no-store", ...init });

export function useFeed(feed, fn) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (!feed?.current) return undefined;
    const f = (ev) => ref.current(ev);
    feed.current.add(f);
    return () => feed.current.delete(f);
  }, [feed]);
}

const mmss = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

// ---------- 4. widgets ----------
function Countdown({ spec }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now() / 1000), 500);
    return () => clearInterval(id);
  }, []);
  const left = Math.max(0, spec.due - now);
  const C = 2 * Math.PI * 40;
  return (
    <div className="wg-count">
      <svg viewBox="0 0 96 96" width="96" height="96" aria-hidden="true">
        <circle cx="48" cy="48" r="40" className="bg" />
        <circle cx="48" cy="48" r="40" className="fg" strokeDasharray={C}
          strokeDashoffset={C * (1 - left / spec.seconds)} transform="rotate(-90 48 48)" />
      </svg>
      <div><b>{left > 0 ? mmss(left) : "done"}</b><span>{spec.title || "timer"}</span></div>
    </div>
  );
}

export function Widget({ spec }) {
  const k = spec.widget;
  let body = null;
  if (k === "countdown") body = <Countdown spec={spec} />;
  else if (k === "forecast") {
    const lo = Math.min(...spec.days.map((d) => d.low)), hi = Math.max(...spec.days.map((d) => d.high));
    const y = (v) => ((v - lo) / Math.max(1, hi - lo)) * 100;
    body = (
      <div className="wg-days">
        {spec.days.map((d) => (
          <div key={d.label} className="wg-day" title={`${d.sky} · ${d.rain}% rain`}>
            <b>{d.high}°</b>
            <span className="wg-span"><i style={{ bottom: `${y(d.low)}%`, top: `${100 - y(d.high)}%` }} /></span>
            <small>{d.low}°</small>
            <span className={`wg-rain${d.rain >= 50 ? " wet" : ""}`}>{d.rain}%</span>
            <em>{d.label}</em>
          </div>
        ))}
      </div>
    );
  } else if (k === "compare") {
    body = (
      <div className="wg-scroll">
        <table className="wg-table">
          <thead><tr><th />{spec.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
          <tbody>{spec.rows.map((r, i) => <tr key={i}>{r.map((c, j) => (j ? <td key={j}>{c}</td> : <th key={j}>{c}</th>))}</tr>)}</tbody>
        </table>
      </div>
    );
  } else if (k === "progress") {
    body = (
      <div className="wg-bars">
        {spec.items.map((it, i) => (
          <div key={i}><span>{it.label}</span><span className="wg-meter"><i style={{ transform: `scaleX(${it.percent / 100})` }} /></span><small>{it.value || `${it.percent}%`}</small></div>
        ))}
      </div>
    );
  } else if (k === "stats") {
    body = <div className="wg-stats">{spec.items.map((it, i) => <div key={i}><b>{it.value}</b><span>{it.label}</span></div>)}</div>;
  } else {
    body = <ul className="wg-list">{spec.items.map((it, i) => <li key={i}><span>{it.label}</span>{it.value && <small>{it.value}</small>}</li>)}</ul>;
  }
  return (
    <figure className={`widget wg-${k}`}>
      {spec.title && k !== "countdown" && <figcaption>{spec.title}</figcaption>}
      {body}
    </figure>
  );
}

// ---------- 2. subagent constellation ----------
export const AGENT_TOOLS = { ask_claude: ["claude", "#ffb347"], ask_opencode: ["opencode", "#ffb347"], web_search: ["web", "#b48bff"],
  fetch_page: ["reader", "#b48bff"], start_job: ["job", "#ffb347"], dream: ["dreamer", "#7dffb0"], run_skill: ["skill", "#7fe7ff"] };

const RIM = 0.4; // where a line starts, as a share of the way from the orb's centre to the node

export function Constellation({ feed, onActive }) {
  const [agents, setAgents] = useState([]); // { key, name, via, color, state, step, at }
  const [pick, setPick] = useState(null);
  const upsert = useCallback((key, patch) => setAgents((a) => {
    const i = a.findIndex((x) => x.key === key);
    if (i < 0) return [...a, { key, at: Date.now(), phase: Math.random() * Math.PI * 2, ...patch }].slice(-7);
    const next = [...a];
    next[i] = { ...next[i], ...patch, pulse: Date.now() };
    return next;
  }), []);
  const finish = useCallback((key, ok, step) => {
    upsert(key, { state: ok ? "done" : "failed", ...(step ? { step } : {}) });
    setTimeout(() => setAgents((a) => a.filter((x) => x.key !== key)), 5000);
  }, [upsert]);
  useFeed(feed, (ev) => {
    const d = ev.data || {};
    if (ev.type === "tool_call" && AGENT_TOOLS[d.tool]) {
      const [name, color] = AGENT_TOOLS[d.tool];
      const what = d.arguments?.question || d.arguments?.query || d.arguments?.url || d.arguments?.goal || d.arguments?.name || "";
      upsert(`${ev.request_id}:${d.tool}`, { name, via: d.tool, color, state: "running", step: String(what).slice(0, 90) || "working" });
    }
    if (ev.type === "tool_result" && AGENT_TOOLS[d.tool])
      finish(`${ev.request_id}:${d.tool}`, d.status === "ok", (d.preview || "").split("\n")[0].slice(0, 90));
    if (ev.type === "job_state") {
      const live = d.state === "running" || d.state === "queued";
      if (live) upsert(`job:${d.id}`, { name: d.skill || "job", via: "background job", color: "#ffb347", state: d.state, step: d.last || d.state });
      else finish(`job:${d.id}`, d.state === "done", d.last || d.state);
    }
    if (ev.type === "skill_run") upsert(`skill:${ev.request_id}`, { name: String(d.skill || "skill").replace(/^skills\//i, ""), via: "skill", color: "#7fe7ff", state: "running", step: d.why || "running" });
    if (ev.type === "skill_done") finish(`skill:${ev.request_id}`, true, (d.text || "").slice(0, 90));
    if (ev.type === "llm_escalated") {
      upsert(`esc:${ev.request_id}`, { name: d.to, via: "bigger model", color: "#ff8ad8", state: "running", step: d.why || "taking over" });
      setTimeout(() => finish(`esc:${ev.request_id}`, true), 15000);
    }
    if (ev.type === "reply") setAgents((a) => a.map((x) => (x.key.startsWith("esc:") && x.key.endsWith(ev.request_id) ? { ...x, state: "done" } : x)));
  });
  const working = agents.some((a) => a.state === "running" || a.state === "queued");
  useEffect(() => onActive?.(working), [working, onActive]);
  const wrap = useRef(null);
  useEffect(() => { // orbit: position each node by time, no React state per frame
    let raf;
    const tick = () => {
      const el = wrap.current;
      if (el) {
        const t = performance.now() / 1000;
        const w = el.clientWidth / 100, h = el.clientHeight / 100; // read before any write: no forced layout
        el.querySelectorAll("[data-node]").forEach((n, i) => {
          const ring = i % 2 ? 0.62 : 0.86, speed = i % 2 ? -0.16 : 0.11;
          const a = Number(n.dataset.phase) + t * speed;
          const x = 50 + 50 * ring * Math.cos(a), y = 50 + 50 * ring * Math.sin(a) * 0.62;
          n.style.transform = `translate(${x * w}px, ${y * h}px) translate(-50%, -50%)`; // compositor-only, unlike left/top
          const line = el.querySelector(`[data-line="${n.dataset.node}"]`);
          // from the orb's rim, not its centre: a line across the orb read as a scratch on it
          if (line) {
            line.setAttribute("x1", 50 + (x - 50) * RIM); line.setAttribute("y1", 50 + (y - 50) * RIM);
            line.setAttribute("x2", x); line.setAttribute("y2", y);
          }
        });
      }
      if (!still) raf = requestAnimationFrame(tick);
    };
    const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
    tick(); // reduced motion: placed once (again whenever an agent comes or goes), standing still
    return () => cancelAnimationFrame(raf);
  }, [agents.length]);
  if (!agents.length) return null;
  const chosen = agents.find((a) => a.key === pick) || agents[agents.length - 1];
  return (
    <div className="constellation" ref={wrap} aria-label="Agents at work">
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
        {agents.map((a) => (
          <line key={a.key} data-line={a.key} x1="50" y1="50" x2="50" y2="50" stroke={a.color}
            className={`${a.state}${a.pulse && Date.now() - a.pulse < 1500 ? " hot" : ""}`} />
        ))}
      </svg>
      {agents.map((a) => (
        <button key={a.key} type="button" data-node={a.key} data-phase={a.phase}
          className={`c-node ${a.state}${pick === a.key ? " on" : ""}`} style={{ "--c": a.color }}
          onClick={(e) => { e.stopPropagation(); setPick(a.key === pick ? null : a.key); }}
          title={`${a.name} · ${a.via} · ${a.step}`}>
          <i aria-hidden="true" /><span>{a.name}</span>
        </button>
      ))}
      <div className="c-detail" style={{ color: chosen.color }}>{chosen.name} › {chosen.state === "done" ? "done: " : ""}{chosen.step}</div>
    </div>
  );
}

// ---------- 3. the note that writes itself ----------
const NOTE_TOOLS = { kb_remember: (a) => [`jarvis/${a.topic || "memory"}`, a.fact], kb_correct: (a) => [`jarvis/${a.topic || "memory"}`, a.fact],
  kb_journal: (a) => ["today's journal", a.text], kb_append_to_page: (a) => [a.page, a.text],
  kb_move_item: (a) => [a.to_page, `moved: ${a.match}`] };

function Typed({ text }) {
  const [n, setN] = useState(0);
  useEffect(() => {
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) return setN(text.length);
    let raf, i = 0, last = performance.now(); // 2 chars every 22 ms, one render per frame
    const tick = (now) => {
      i = Math.min(text.length, i + ((now - last) / 22) * 2);
      last = now;
      setN(Math.floor(i));
      if (i < text.length) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [text]);
  const parts = text.slice(0, n).split(/(\[\[[^\]]+\]\])/g);
  return (
    <>{parts.map((p, i) => (p.startsWith("[[") && p.endsWith("]]") ? <a key={i}>{p.slice(2, -2)}</a> : p))}
      {n < text.length && <span className="caret" aria-hidden="true" />}</>
  );
}

export function LiveNote({ feed, pinned, focused, head }) {
  const [lines, setLines] = useState([]); // { id, page, text, task }
  const [open, setOpen] = useState(false);
  const pending = useRef(new Map());
  const timer = useRef(null);
  useFeed(feed, (ev) => {
    const d = ev.data || {};
    if (ev.type === "tool_call" && NOTE_TOOLS[d.tool]) pending.current.set(`${ev.request_id}:${d.tool}`, NOTE_TOOLS[d.tool](d.arguments || {}));
    if (ev.type === "tool_result" && NOTE_TOOLS[d.tool]) {
      const key = `${ev.request_id}:${d.tool}`, hit = pending.current.get(key);
      pending.current.delete(key);
      if (!hit || (d.status !== "ok" && d.status !== "dry-run") || !hit[1]) return;
      const text = String(hit[1]).replace(/^\s*-\s*/, "");
      setLines((l) => [...l, { id: `${key}:${Date.now()}`, page: hit[0] || "notes", task: /^\s*(\[ \]|TODO)\b/i.test(text),
        text: text.replace(/^\s*(\[ \]|TODO)\s*/i, "") }].slice(-8));
      setOpen(true);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setOpen(false), 45000); // folds away when the writing stops
    }
  });
  useEffect(() => () => clearTimeout(timer.current), []);
  if (!lines.length) return null;
  const links = [...new Set(lines.flatMap((l) => [l.page, ...[...l.text.matchAll(/\[\[([^\]]+)\]\]/g)].map((m) => m[1])]))];
  return (
    <section className={`panel live-note${open || pinned ? " open" : ""}${focused ? " focused" : ""}`}
      aria-label="Notes being written">
      {/* the heading expands it like the other panels (wider column, full height); expanded, it never folds */}
      <h2 {...head}>Writing to notes <small>{lines.length}<b className="expand" aria-hidden="true" /></small></h2>
      {(open || pinned) && (
        <div className="ln-body">
          <ol className="ln-lines">
            {lines.map((l) => (
              <li key={l.id} className={l.task ? "task" : ""}><Typed text={l.text} /><small>{l.page}</small></li>
            ))}
          </ol>
          <div className="ln-rail"><span>linked</span>{links.map((p) => <i key={p}>{p}</i>)}</div>
        </div>
      )}
    </section>
  );
}

// ---------- 6. pop-ups that fade by importance ----------
const LIFE = { info: 4000, done: 6000, warn: 12000 };

export function Toasts({ feed }) {
  const [toasts, setToasts] = useState([]); // { id, kind, title, text }
  const [missed, setMissed] = useState([]);
  const [show, setShow] = useState(false);
  const add = useCallback((kind, title, text) => {
    const id = `${Date.now()}-${Math.random()}`;
    setToasts((t) => [{ id, kind, title, text, at: Date.now() }, ...t].slice(0, 5));
    setTimeout(() => setToasts((t) => {
      const gone = t.find((x) => x.id === id);
      if (gone && !gone.seen) setMissed((m) => [{ ...gone, at: Date.now() }, ...m].slice(0, 30));
      return t.filter((x) => x.id !== id);
    }), LIFE[kind] * (document.hidden ? 0.3 : 1));
  }, []);
  useFeed(feed, (ev) => {
    const d = ev.data || {};
    if (ev.type === "notify") add("warn", d.title || "Notice", d.text);
    if (ev.type === "hook_event") add("info", `webhook · ${d.kind}`, d.text);
    if (ev.type === "skill_done" && !d.dry_run) add("done", `skill · ${String(d.skill || "").replace(/^skills\//i, "")}`, d.text);
    if (ev.type === "timer") add("warn", "Timer", `${d.label || "timer"} is done`);
    if (ev.type === "job_state" && ["done", "failed"].includes(d.state)) add(d.state === "done" ? "done" : "warn", `job ${d.state}`, d.last || d.id);
    if (ev.type === "watch" && d.kind === "fired") add("warn", `watch · ${d.name}`, d.text || "something new");
    if (ev.type === "approval_request") add("ask", "Needs you", d.says || d.tool);
  });
  const keep = (id) => setToasts((t) => t.map((x) => (x.id === id ? { ...x, seen: true } : x)));
  const close = (id) => setToasts((t) => t.filter((x) => x.id !== id));
  return (
    <div className="toasts" aria-live="polite">
      {missed.length > 0 && (
        <div className="away">
          <button type="button" className="away-chip" aria-expanded={show} onClick={() => setShow((s) => !s)}>
            while you were away · {missed.length}
          </button>
          {show && (
            <ul className="away-list">
              {missed.map((m) => <li key={m.id} className={m.kind}><b>{m.title}</b> {m.text}</li>)}
              <li><button type="button" onClick={() => { setMissed([]); setShow(false); }}>Clear</button></li>
            </ul>
          )}
        </div>
      )}
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`} onMouseEnter={() => keep(t.id)} onFocus={() => keep(t.id)} tabIndex={0}>
          <b>{t.title}</b><span>{t.text}</span>
          {t.kind === "ask" ? <small>see the approval card</small> : <i className="life" style={{ animationDuration: `${LIFE[t.kind]}ms` }} />}
          <button type="button" aria-label="Dismiss" onClick={() => close(t.id)}>×</button>
        </div>
      ))}
    </div>
  );
}

// ---------- 7. morning memory digest ----------
const OPS = [["added", "Learned"], ["merged", "Merged"], ["updated", "Updated"], ["moved", "Moved"]];

// what last night's dream changed on the memory pages (each with undo) and the questions it has for the user
export function DreamDigest() {
  const [rep, setRep] = useState(null);
  const [questions, setQuestions] = useState([]);
  const [hidden, setHidden] = useState(true);
  useEffect(() => {
    api("dream").then((r) => (r.ok ? r.json() : null)).then((d) => {
      const last = d?.last;
      const fresh = last?.changes?.length && !last.dry_run && Date.now() / 1000 - last.started < 20 * 3600;
      const open = d?.questions || [];
      if (!fresh && !open.length) return;
      let seen = null;
      try { seen = localStorage.getItem("jarvis.digest"); } catch { /* storage blocked: show it */ }
      setRep(fresh ? last : null);
      setQuestions(open);
      setHidden(!open.length && seen === String(last.started)); // an open question stays until answered
    }).catch(() => null);
  }, []);
  if (hidden || (!rep && !questions.length)) return null;
  const undo = async (i) => {
    const r = await api(`dream/undo/${i}`, { method: "POST" }).catch(() => null);
    if (r?.ok) { const c = await r.json(); setRep((x) => ({ ...x, changes: x.changes.map((y, j) => (j === i ? c : y)) })); }
  };
  const answer = async (id, choice, text = "") => {
    const r = await api(`dream/answer/${id}`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ choice, text }),
    }).catch(() => null);
    if (r?.ok) setQuestions((qs) => qs.filter((q) => q.id !== id));
  };
  const close = () => { try { localStorage.setItem("jarvis.digest", String(rep?.started)); } catch { /* fine */ } setHidden(true); };
  return (
    <section className="digest" aria-label="Last night's memory changes">
      <div className="digest-head"><span>Overnight · what I remembered</span><button type="button" onClick={close} aria-label="Dismiss">×</button></div>
      {rep?.themes && <p className="digest-themes">{rep.themes}</p>}
      {rep && (
        <div className="digest-cols">
          {OPS.map(([op, label]) => {
            const items = rep.changes.map((c, i) => [c, i]).filter(([c]) => c.op === op);
            return items.length ? (
              <div key={op} className={`digest-col ${op}`}>
                <h3>{label}</h3>
                {items.map(([c, i]) => (
                  <div key={i} className={`fact${c.undone ? " undone" : ""}`}>
                    <span>{c.text}</span>
                    {op === "moved" ? <small>to {c.page?.replace("jarvis/", "")}, from {c.was?.[0]?.page?.replace("jarvis/", "")}</small>
                      : Array.isArray(c.was) && c.was.length > 0 && <small>was: {c.was.map((w) => w.text).join(" · ")}</small>}
                    <button type="button" onClick={() => undo(i)}>{c.undone ? "Put back" : "Undo"}</button>
                  </div>
                ))}
              </div>
            ) : null;
          })}
        </div>
      )}
      {questions.length > 0 && (
        <div className="digest-qs">
          <h3>Questions for you</h3>
          {questions.map((q) => <Question key={q.id} q={q} answer={answer} />)}
        </div>
      )}
    </section>
  );
}

function Question({ q, answer }) {
  const [text, setText] = useState("");
  if (q.a) {
    return (
      <div className="fact q">
        <span>These disagree. Which is right?</span>
        <button type="button" onClick={() => answer(q.id, "a")}>{q.a}</button>
        <button type="button" onClick={() => answer(q.id, "b")}>{q.b}</button>
        <button type="button" className="ignore" onClick={() => answer(q.id, "dismiss")}>Both fine · don't ask again</button>
      </div>
    );
  }
  return (
    <form className="fact q" onSubmit={(e) => { e.preventDefault(); if (text.trim()) answer(q.id, "text", text); }}>
      <label htmlFor={`q-${q.id}`}>{q.q}</label>
      <input id={`q-${q.id}`} value={text} onChange={(e) => setText(e.target.value)} placeholder="Your answer" maxLength={300} />
      <span className="q-actions">
        <button type="submit" disabled={!text.trim()}>Save</button>
        <button type="button" className="ignore" onClick={() => answer(q.id, "dismiss")}>Ignore</button>
      </span>
    </form>
  );
}

// ---------- 8. what an approval would do ----------
export function Preview({ p }) {
  if (!p) return null;
  if (p.kind === "targets") {
    return (
      <div className="pv pv-targets">
        <span className="pv-label">will {p.action}</span>
        {p.items.map((t) => <span key={t} className="pv-target"><i aria-hidden="true" />{t}</span>)}
      </div>
    );
  }
  return (
    <div className="pv pv-diff">
      <span className="pv-label">{p.page}</span>
      {p.lines.map((l, i) => <code key={i} className={l.op === "+" ? "ins" : l.op === "-" ? "del" : ""}>{l.op} {l.text}</code>)}
      {p.more > 0 && <small>and {p.more} more</small>}
    </div>
  );
}

// ---------- 9. the wit dial ----------
const SAMPLE = ["Ten minutes on the pasta.", "Ten minutes on the pasta, sir. I shall resist the urge to stir.",
  "Ten minutes on the pasta, sir. Al dente waits for no one; I'll give you a shout."];
const NAMES = ["plain", "dry", "playful"];

export function WitPanel() {
  const [st, setSt] = useState(null);
  const [err, setErr] = useState("");
  const load = useCallback(() => api("wit").then((r) => (r.ok ? r.json() : Promise.reject())).then(setSt)
    .catch(() => setErr("The hub didn't answer.")), []);
  useEffect(() => { load(); }, [load]);
  const set = async (level, satellite = "", minutes = null) => {
    const r = await api("wit", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ level, satellite, minutes }) }).catch(() => null);
    if (r?.ok) { setSt(await r.json()); setErr(""); } else setErr("Couldn't change it.");
  };
  const clear = async (satellite) => {
    const r = await api(`wit?satellite=${encodeURIComponent(satellite)}`, { method: "DELETE" }).catch(() => null);
    if (r?.ok) setSt(await r.json());
  };
  if (!st) return <p className="tools-note">{err || "loading…"}</p>;
  const rooms = Object.entries(st.satellites || {});
  return (
    <div className="wit-panel">
      <p className="tools-note">How much humour JARVIS allows itself. Also by voice: “be more serious”, “dial the humour up for an hour”, “no jokes in here”.</p>
      <label className="wit-dial" htmlFor="wit-house">
        <span>plain</span>
        <input id="wit-house" type="range" min="0" max="2" step="1" value={st.level} onChange={(e) => set(Number(e.target.value))} />
        <span>playful</span>
      </label>
      <div className="wit-sample"><b>{NAMES[st.level]}{st.until ? ` · until ${new Date(st.until * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : ""}</b>{SAMPLE[st.level]}</div>
      <div className="wit-rooms">
        <span>rooms</span>
        {rooms.length === 0 && <small>every room follows the house</small>}
        {rooms.map(([id, v]) => (
          <span key={id} className={`wit-room lv${v.level}`}>
            {id.replace(/^satellite:/, "")} · {NAMES[v.level]}
            <button type="button" aria-label={`Back to the house level for ${id}`} onClick={() => clear(id)}>×</button>
          </span>
        ))}
      </div>
      {err && <p className="tools-note">{err}</p>}
    </div>
  );
}
