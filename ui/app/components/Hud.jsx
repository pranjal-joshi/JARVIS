"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { BorderBeam } from "border-beam";
import dynamic from "next/dynamic";
import Orb from "./Orb";
import BootScreen from "./BootScreen";
import RichText from "./RichText";
import { headProps } from "../lib/panel";
import SystemConsole, { asCommand } from "./SystemConsole";
import MonitorsPanel from "./MonitorsPanel";
import JobBox from "./JobBox";
import { AGENT_TOOLS, Constellation, DreamDigest, Health, LiveNote, Preview, Toasts, WitPanel, Widget } from "./Live";
import { SOURCE, ago, describe, insight } from "../lib/timeline";
import { askToNotify, chime, heardSpeech, notifyApproval, notifySystem, playAudio, startRecording, stopAudio } from "../lib/audio";

// WebGL graph: client-only so the server build never touches three.js
const KbGraph = dynamic(() => import("./KbGraph"), {
  ssr: false,
  loading: () => <div className="kb-canvas kb-loading">initialising graph…</div>,
});

const KbExplorer = dynamic(() => import("./KbExplorer"), { ssr: false });
const DevicesPanel = dynamic(() => import("./DevicesPanel"), { ssr: false });
const SkillsPanel = dynamic(() => import("./SkillsPanel"), { ssr: false });
const GuardPanel = dynamic(() => import("./GuardPanel"), { ssr: false });
const WatchPanel = dynamic(() => import("./WatchPanel"), { ssr: false });
const WebhooksPanel = dynamic(() => import("./WebhooksPanel"), { ssr: false });
const PAGE_MIME = "application/x-jarvis-page"; // a page dragged from the explorer

const api = (path, init) => fetch(`/api/hub/${path}`, { cache: "no-store", ...init });

// Model picker names: where each model runs matters (a cloud model sees the conversation).
const MODEL_NAMES = { "claude-haiku-5-5": "Haiku 5.5", "claude-sonnet-5-5": "Sonnet 5.5", "claude-opus-5-5": "Opus 5.5" };
const modelLabel = (m) => (m.startsWith("claude-")
  ? `${MODEL_NAMES[m] || m} · cloud`
  : /^(groq|zai)-/.test(m) ? `${m} · cloud`
  : m.startsWith("unsloth-") ? `${m} · DGX`
  : `${m.replace(/^hf\.co\/[^/]+\//, "").replace(/-GGUF:.*$/i, "")} · local`);

// Pages the user attached are sent with the question, marked as their own notes.
const withContext = (text, pages) => (pages.length
  ? pages.map((p) => `[Note from my knowledge base: "${p.name}"]\n${p.text.slice(0, 3000)}`).join("\n\n")
    + `\n\n[My question]\n${text}`
  : text);

// Server-sent events from a fetch body, one parsed JSON object per `data:` line.
async function* sse(body) {
  const reader = body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, i);
      buf = buf.slice(i + 2);
      for (const line of block.split("\n")) {
        if (!line.startsWith("data:")) continue;
        const d = line.slice(5).trim();
        if (d === "[DONE]") return;
        try { yield JSON.parse(d); } catch { /* not JSON: skip */ }
      }
    }
  }
}

// End of the first complete sentence in s (outside code fences), or 0 if none yet.
function sentenceEnd(s) {
  const re = /[.!?…]+["')\]]?\s+|\n\s*\n/g;
  let m;
  while ((m = re.exec(s))) {
    const end = m.index + m[0].length;
    const head = s.slice(0, end);
    if ((head.match(/```/g) || []).length % 2) continue; // inside a code block
    if (head.trim().length >= 12) return end;
  }
  return 0;
}

function useTypewriter(text, speed = 14) {
  const [shown, setShown] = useState("");
  const prev = useRef({ text: "", i: 0 }); // a streamed reply grows: carry on typing, don't restart
  useEffect(() => {
    if (!text) {
      setShown("");
      prev.current = { text: "", i: 0 };
      return;
    }
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setShown(text);
      return;
    }
    let i = text.startsWith(prev.current.text) ? prev.current.i : 0;
    prev.current = { text, i };
    // one render per display frame (not per timer tick): 2 chars every `speed` ms
    let raf, last = performance.now();
    const tick = (now) => {
      i = Math.min(text.length, i + ((now - last) / speed) * 2);
      last = now;
      prev.current.i = Math.floor(i);
      setShown(text.slice(0, Math.floor(i)));
      if (i < text.length) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [text, speed]);
  return shown;
}

const reducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// Short "decode" reveal: random glyphs resolve left-to-right into the label.
const GLYPHS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789#%/+=";
function Decode({ label, duration = 420 }) { // its own component: the per-frame updates re-render only this span
  const [shown, setShown] = useState(label);
  useEffect(() => {
    if (reducedMotion()) {
      setShown(label);
      return;
    }
    const start = performance.now();
    let raf;
    const tick = (now) => {
      const p = Math.min(1, (now - start) / duration);
      const fixed = Math.floor(p * label.length);
      let out = label.slice(0, fixed);
      for (let i = fixed; i < label.length; i++) out += GLYPHS[(Math.random() * GLYPHS.length) | 0];
      setShown(out);
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [label, duration]);
  return <span aria-hidden="true">{shown}</span>;
}

// Which KB pages an event touched, and whether it was a read or a write.
// kind picks the map colour: read cyan, tasks green, write amber, erase red, fault (blocked/failed) pink.
const KB_WRITES = new Set(["kb_remember", "kb_journal", "kb_append_to_page"]);
const KB_ERASES = new Set(["kb_forget", "kb_clear_memory", "kb_delete_page", "kb_delete_item"]);
function kbTouch(ev) {
  if (ev.type === "kb_retrieved") return { pages: ev.data?.pages || [], kind: "read", tool: "kb_search" };
  const tool = ev.data?.tool;
  if ((ev.type !== "tool_call" && ev.type !== "tool_result") || !tool?.startsWith("kb_")) return null;
  const st = ev.type === "tool_result" ? ev.data.status : null;
  const kind = st && !["ok", "dry-run"].includes(st) ? "fault"
    : KB_ERASES.has(tool) ? "erase" : KB_WRITES.has(tool) ? "write" : tool === "kb_tasks" ? "tasks" : "read";
  const pages = [];
  if (ev.type === "tool_call") {
    const args = ev.data.arguments || {};
    if (args.page) pages.push(String(args.page));
    if (tool === "kb_remember") {
      const topic = String(args.topic || "memory").trim() || "memory";
      pages.push(topic.toLowerCase().startsWith("jarvis/") ? topic : `jarvis/${topic}`);
    }
  } else if (typeof ev.data.preview === "string") {
    const p = ev.data.preview;
    for (const m of p.matchAll(/\[\[([^\]\n]{1,120})\]\]/g)) pages.push(m[1]); // [[page]]
    // "[page] text" search hits and "... [page]" task lines
    for (const m of p.matchAll(/(?:^|\n)\[([^[\]\n]{1,120})\]|\[([^[\]\n]{1,120})\][ \t]*(?=\n|$)/g)) pages.push(m[1] || m[2]);
    const j = p.match(/journal (.+?)\.\s*$/);
    if (j) pages.push(j[1]);
  }
  return { pages, kind, tool, status: st,
    label: `${describe(tool, ev.data.arguments).title} · ${st ? insight(tool, st, ev.data.preview) : "…"}` };
}

// The "why" line for a reply: which route answered it, the tools and who let them run, the time.
const ROUTES = { fast: "fast path (no model)", memory: "memory capture", confirm: "spoken confirmation", model: "model" };
function why(t) {
  const route = t.route === "model" && t.model ? `model ${t.model}` : ROUTES[t.route] || t.route;
  const tools = (t.tools || []).map((x) => `${x.tool} (${x.decision}${x.rule && x.rule !== x.decision ? `: ${x.rule}` : ""})`);
  return `↳ ${route}${t.intent ? ` · ${t.intent}` : ""} · ${t.seconds} s${tools.length ? ` · ${tools.join(", ")}` : ""}`
    + `${t.dry_run ? " · dry run" : ""}`;
}

// Stable keys so newly pushed timeline rows animate in (index keys would not).
let seq = 0;
const withId = (item) => ({ ...item, id: ++seq });

function Message({ m, latest }) {
  const typed = useTypewriter(latest && m.role === "assistant" ? m.text : "");
  const text = latest && m.role === "assistant" ? typed : m.text;
  return (
    <div className={`msg ${m.role}`} role={m.role === "alert" ? "alert" : undefined}>
      <span className="who">{m.role === "user" ? "YOU" : m.role === "alert" ? "ALERT" : "JARVIS"}</span>
      <div className="text">
        {m.skill && <div className="msg-ctx"><span>⚙ skill · {m.skill.replace(/^skills\//i, "")}</span></div>}
        {m.pages?.length > 0 && <div className="msg-ctx">{m.pages.map((n) => <span key={n}>◈ {n}</span>)}</div>}
        <RichText text={text} />
      </div>
    </div>
  );
}

// line icons, drawn on a 24px grid so they sit dead centre in the round button
const MicIcon = () => (
  <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none" stroke="currentColor"
    strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <rect x="9" y="3" width="6" height="11" rx="3" />
    <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7" />
  </svg>
);
const StopIcon = () => (
  <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="2.5" fill="currentColor" /></svg>
);

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export default function Hud() {
  const [state, setState] = useState("idle");
  const [analyser, setAnalyser] = useState(null);
  const [messages, setMessages] = useState([]);
  const feed = useRef(new Set()); // the live parts (Live.jsx) listen to the one event stream through this
  const [input, setInput] = useState("");
  const [timeline, setTimeline] = useState([]);
  const [approvals, setApprovals] = useState([]);
  const [status, setStatus] = useState(null);
  const [models, setModels] = useState([]);
  const [switching, setSwitching] = useState(false);
  const [link, setLink] = useState("connecting");
  const [speak, setSpeak] = useState(true);
  const speakRef = useRef(null); // lets the long-lived event handler speak (timer alerts)
  const [leaving, setLeaving] = useState(() => new Set());
  const [kbPulse, setKbPulse] = useState(null);
  const [kbActive, setKbActive] = useState(false);
  // monitors (watchers, timers, schedules): the panel grows and the changed row animates on each change
  const [monitors, setMonitors] = useState(null);
  const [monPulse, setMonPulse] = useState(null); // { name, kind, seq }
  const [monActive, setMonActive] = useState(false);
  const [ghosts, setGhosts] = useState([]); // cancelled / deleted / done rows, kept a moment to dissolve
  const monTimer = useRef(null);
  const loadMonitors = useCallback(async () => {
    const r = await api("monitors").catch(() => null);
    if (r?.ok) setMonitors(await r.json());
  }, []);
  // background jobs: while any card is up the orb moves aside and the cards stack beside it. A card stays
  // until it is dismissed (a running one can be stopped); chat, new questions and panel focus leave them be.
  const [closingJobs, setClosingJobs] = useState(() => new Set()); // dismissed here, playing their exit
  const jobCards = (monitors?.jobs || []).filter((j) => j.state === "running" || j.state === "queued" || !j.dismissed);
  const dismissJob = useCallback((id) => {
    setClosingJobs((s) => new Set(s).add(id));
    setTimeout(async () => { // after the card's exit animation
      await api(`jobs/${id}/dismiss`, { method: "POST" }).catch(() => null);
      await loadMonitors();
      setClosingJobs((s) => { const n = new Set(s); n.delete(id); return n; });
    }, 420);
  }, [loadMonitors]);
  const cancelJob = useCallback(async (id) => {
    await api(`jobs/${id}/cancel`, { method: "POST" }).catch(() => null);
    loadMonitors();
  }, [loadMonitors]);
  const jobOn = jobCards.some((j) => !closingJobs.has(j.id));
  const [orbAside, setOrbAside] = useState(false); // follows jobOn, as a view transition like the other layout moves
  useEffect(() => {
    if (!document.startViewTransition || reducedMotion() || document.querySelector(".boot")) return setOrbAside(jobOn);
    const vt = document.startViewTransition(() => flushSync(() => setOrbAside(jobOn)));
    vt.ready.catch(() => {});
    vt.finished.catch(() => {});
    return undefined;
  }, [jobOn]);
  const [kbVersion, setKbVersion] = useState(0);
  const [kbView, setKbView] = useState("normal"); // normal | min | max
  const [context, setContext] = useState([]); // [{ name, text }] attached to the next question
  const addContext = useCallback((p) => setContext((c) => (
    c.some((x) => x.name.toLowerCase() === p.name.toLowerCase()) ? c : [...c, p].slice(-4))), []);
  const closeKb = useCallback(() => setKbView("normal"), []);
  const [dropping, setDropping] = useState(false);
  const dropPage = async (e) => {
    e.preventDefault();
    setDropping(false);
    const name = e.dataTransfer.getData(PAGE_MIME);
    if (!name) return;
    const r = await api(`kb/page?name=${encodeURIComponent(name)}`).catch(() => null);
    if (r?.ok) {
      const pg = await r.json();
      addContext({ name: pg.name, text: pg.blocks.map((b) => `- ${b}`).join("\n") });
    }
  };
  const [kbStats, setKbStats] = useState(null);
  // start-up: each source reports ok / fail; the boot screen leaves once all have, or after 12 s
  const [boot, setBoot] = useState({ hub: null, audit: null, events: null, graph: null });
  const mark = useCallback((k, v) => setBoot((b) => (b[k] === v ? b : { ...b, [k]: v })), []);
  const [bootPhase, setBootPhase] = useState("on"); // on -> leaving -> off
  const kbTimer = useRef(null);
  const [consoleLines, setConsoleLines] = useState([]);
  const [consoleActive, setConsoleActive] = useState(false);
  const [accent, setAccent] = useState(null); // orb colour flash: { color, at }
  const flash = (color) => setAccent({ color, at: Date.now() });
  const consoleTimer = useRef(null);
  const recording = useRef(null);
  const byVoice = useRef(false); // the current turn was spoken: its spoken reply gets a follow-up window
  const turn = useRef(0); // newest question; older replies show but never speak over it
  const sessionId = useRef(null); // this conversation's id: the hub keeps its turns under it (traces)
  if (sessionId.current === null) sessionId.current = newSessionId();
  const chatRef = useRef(null);
  const [tab, setTab] = useState("talk"); // phone layout: talk | activity | watch | map
  // Desktop focus: the panel JARVIS is working in (or one the user pinned) widens and the orb
  // docks aside. The dock holds through the answer (thinking, then speaking) and is let go once
  // playback ends; listening always brings the orb back to centre.
  const [devCount, setDevCount] = useState(null);
  const [skillOpen, setSkillOpen] = useState(false);
  const [skillCount, setSkillCount] = useState(null);
  const [watchOpen, setWatchOpen] = useState(false);
  // the less-used panels (webhooks, guard, devices, tools) share one ⚙ menu with tabs
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState("webhooks");
  const [llmInfo, setLlmInfo] = useState(null); // { loaded, median_s }: the model badge
  const [hookSeq, setHookSeq] = useState(0); // bumps on hook events so open panels refresh
  const notifiedRuns = useRef(new Set()); // skill runs whose notification is already in the chat
  const openOnly = (which) => { // one header dropdown at a time
    setSkillOpen(which === "skill"); setWatchOpen(which === "watch"); setSettingsOpen(which === "settings");
  };
  useEffect(() => { // close the tools list on any click outside it
    if (!skillOpen && !watchOpen && !settingsOpen) return;
    const off = (e) => { if (!e.target.closest?.(".tools-pick")) openOnly(null); };
    document.addEventListener("pointerdown", off);
    return () => document.removeEventListener("pointerdown", off);
  }, [skillOpen, watchOpen, settingsOpen]);
  const [agentsOn, setAgentsOn] = useState(false);
  const [pinned, setPinned] = useState(null); // timeline | console | kb | watch | notes
  const togglePin = (id) => setPinned((p) => (p === id ? null : id));
  const lastBusy = useRef(null);
  // any knowledge-base tool enlarges the map; a watcher or timer changing, the monitors; other tools the console
  const busy = kbActive ? "kb" : monActive ? "watch" : consoleActive ? "console" : null;
  const holding = state === "thinking" || state === "speaking";
  if (busy) lastBusy.current = busy;
  else if (!holding) lastBusy.current = null;
  // agents at work (web, subagents, jobs): the orb stays big with them orbiting it, not docked behind the console
  const auto = state === "listening" || agentsOn ? null : busy || (holding ? lastBusy.current : null);
  const focus = pinned || auto;
  // Layout changes (focus, panels growing) run as one view transition: the browser snapshots the
  // old and new layout and animates them on the compositor, so the move stays at 60 fps instead of
  // re-laying out the grid and resizing the WebGL graph on every frame.
  const layoutKey = `${focus}|${kbActive}|${consoleActive}|${monActive}`;
  const [shown, setShown] = useState({ focus, kb: kbActive, con: consoleActive, mon: monActive });
  useEffect(() => {
    const next = { focus, kb: kbActive, con: consoleActive, mon: monActive };
    // no transition under the boot screen: its snapshots would paint above it
    if (!document.startViewTransition || reducedMotion() || window.innerWidth <= 1000
      || document.querySelector(".boot")) return setShown(next);
    const vt = document.startViewTransition(() => flushSync(() => setShown(next)));
    // a hidden tab or a newer change skips the animation (the update still applies): not an error
    vt.ready.catch(() => {});
    vt.finished.catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layoutKey]);
  const newSession = () => {
    turn.current++;
    sessionId.current = newSessionId();
    stopAudio();
    recording.current?.stop().catch(() => {});
    recording.current = null;
    setAnalyser(null);
    setMessages([]);
    setConsoleLines([]);
    setInput("");
    setPinned(null);
    setState("idle");
  };
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") { setPinned(null); setToolsOpen(false); setDevOpen(false); setSkillOpen(false); } };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const push = (item) => setTimeline((t) => [withId(item), ...t].slice(0, 80));
  // update the newest still-open row for this request + tool (a call and its result share one row)
  const settle = (req, tool, patch) =>
    setTimeline((t) => {
      const i = t.findIndex((r) => r.req === req && (!tool || r.tool === tool) && (r.status === "running" || r.status === "pending"));
      if (i < 0) return t;
      const next = [...t];
      next[i] = { ...next[i], ...patch };
      return next;
    });
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => { // model badge: warm or cold, and how long replies take lately
    const poll = async () => {
      const r = await api("llm").catch(() => null);
      if (r?.ok) { const j = await r.json(); setLlmInfo({ loaded: j.loaded, median_s: j.median_s }); }
    };
    const id = setInterval(poll, 30000);
    return () => clearInterval(id);
  }, []);
  const waiting = approvals.length > 0;
  useEffect(() => { // the monitors window: now, then every 30 s (events refresh it in between)
    loadMonitors();
    const id = setInterval(loadMonitors, 30000);
    return () => clearInterval(id);
  }, [loadMonitors]);
  useEffect(() => { // every second while an approval card counts down, else every 15 s
    const id = setInterval(() => setNow(Date.now() / 1000), waiting ? 1000 : 15000);
    return () => clearInterval(id);
  }, [waiting]);

  // console: append lines. Only our tools light the window up (and let it settle after a few quiet
  // seconds); "why" lines, hooks and skill notes are written without growing it.
  const note = (...lines) => setConsoleLines((c) => [...c, ...lines.map(withId)].slice(-120));
  const echo = (...lines) => {
    note(...lines);
    setConsoleActive(true);
    clearTimeout(consoleTimer.current);
    consoleTimer.current = setTimeout(() => setConsoleActive(false), 6000);
  };

  // initial data
  useEffect(() => {
    (async () => {
      const safe = (path) => api(path).catch(() => ({ ok: false }));
      // each source reports on its own: the slow status check (it pings the LLM and voice) must not
      // hold back the rest
      const parse = (x) => { try { return JSON.parse(x || "{}"); } catch { return {}; } };
      await Promise.all([
        safe("audit?limit=30").then(async (a) => {
          mark("audit", a.ok ? "ok" : "fail");
          if (a.ok) mark("hub", "ok"); // the hub answered: linked, even before the slower status check
          if (!a.ok) return;
          const rows = await a.json();
          setTimeline(rows.map((r) => withId({ ts: r.ts, req: r.request_id, tool: r.tool, args: parse(r.arguments),
            risk: r.risk, status: r.status, preview: r.result || "", source: r.source, by: r.approved_by })));
        }),
        safe("approvals").then(async (p) => p.ok && setApprovals(await p.json())),
        safe("llm").then(async (l) => {
          if (!l.ok) return;
          const j = await l.json();
          setModels(j.models || []);
          setLlmInfo({ loaded: j.loaded, median_s: j.median_s });
        }),
        safe("status").then(async (st) => {
          if (st.ok) mark("hub", "ok");
          if (st.ok) setStatus(await st.json());
        }),
      ]);
    })();
  }, [mark]);

  useEffect(() => { if (link === "online") mark("events", "ok"); }, [link, mark]);
  useEffect(() => { if (kbStats) mark("graph", "ok"); }, [kbStats, mark]);
  useEffect(() => {
    const ready = Object.values(boot).every((v) => v);
    if (bootPhase !== "on") return;
    const t = setTimeout(() => setBootPhase("leaving"), ready ? 450 : 12000);
    return () => clearTimeout(t);
  }, [boot, bootPhase]);
  useEffect(() => {
    if (bootPhase !== "leaving") return;
    const t = setTimeout(() => setBootPhase("off"), reducedMotion() ? 0 : 700);
    return () => clearTimeout(t);
  }, [bootPhase]);

  // approval notifications while the HUD is in the background: permission is asked on the first click
  useEffect(() => {
    addEventListener("pointerdown", askToNotify, { once: true });
    return () => removeEventListener("pointerdown", askToNotify);
  }, []);

  // event stream (reconnects with a fresh one-time ticket)
  useEffect(() => {
    let ws;
    let stop = false;
    let retry;
    const connect = async () => {
      try {
        const r = await fetch("/api/events-ticket", { cache: "no-store" });
        if (!r.ok) throw new Error("ticket");
        const { ticket, wsUrl } = await r.json();
        if (stop) return; // unmounted while fetching the ticket
        ws = new WebSocket(wsUrl);
        ws.onopen = () => ws.send(JSON.stringify({ ticket }));
        ws.onmessage = (e) => {
          const ev = JSON.parse(e.data);
          if (ev.type === "subscribed") return setLink("online");
          feed.current.forEach((f) => { try { f(ev); } catch (err) { console.error(err); } });
          if (ev.type === "widget") setMessages((m) => [...m, { role: "widget", spec: ev.data }]); // drawn from the kit
          if (ev.type === "thinking") setState("thinking");
          if (ev.type === "llm_fallback") { // the model was rate limited: the hub moved to the fallback
            const d = ev.data || {};
            setStatus((s) => ({ ...s, llm: { ...s?.llm, model: d.to } }));
            setMessages((m) => [...m, { role: "alert", text: `Rate limited on ${d.from}: switched to ${d.to}. `
              + `${d.from} takes requests again in about ${Math.max(1, Math.round(d.retry_s || 60))} s; `
              + "JARVIS switches back after that." }]);
            flash("red");
          }
          if (ev.type === "llm_restored") { // the rate limit passed: back on the model picked before
            const d = ev.data || {};
            setStatus((s) => ({ ...s, llm: { ...s?.llm, model: d.to } }));
            note({ kind: "info", text: `Back on ${d.to}: its rate limit has passed.` });
          }
          if (ev.type === "tool_call" && ev.data.risk && ev.data.risk !== "read") flash("orange");
          if (ev.type === "approval_request") { flash("orange"); chime(); notifyApproval(ev.data); }
          if (ev.type === "hook_event") {
            setHookSeq((n) => n + 1);
            note({ kind: "info", text: `⚡ ${ev.data.kind}: ${ev.data.text}` });
          }
          if (ev.type === "skill_run")
            note({ kind: "info", text: `⚙ skill "${ev.data.skill}" (${ev.data.why})${ev.data.dry_run ? " · dry run" : ""}` });
          if (ev.type === "skill_done") {
            setHookSeq((n) => n + 1);
            if (ev.data.dry_run) { // nothing was done: show what would have been, don't speak it
              note({ kind: "info", text: `⚙ dry run of "${ev.data.skill}" would have: ${(ev.data.would || []).join("; ") || "done nothing"}` });
            } else {
              const line = ev.data.text || "Done.";
              // a run that notified already has its 🔔 card in the chat: its reply only restates it, so it's
              // spoken but not shown a second time
              if (!notifiedRuns.current.delete(ev.request_id))
                setMessages((m) => [...m, { role: "assistant", text: line, skill: ev.data.skill }]);
              if (speakRef.current) speakRef.current(line);
            }
          }
          if (ev.type === "trace") note({ kind: "info", text: why(ev.data) }); // how the reply came about
          if (ev.type === "watch" || ev.type === "timer_state" || ev.type === "job_state") {
            const d = ev.data;
            const name = ev.type === "timer_state" ? d.label : ev.type === "job_state" ? d.id : d.name;
            loadMonitors();
            // a job has its own box beside the orb: its events only refresh the list, they don't dock the orb
            if (ev.type !== "job_state" && !(ev.type === "watch" && (d.kind === "running" || (d.kind === "ran" && !d.new)))) {
              const seq = Date.now() + Math.random();
              setMonPulse({ name, kind: d.kind, seq });
              if (["cancelled", "deleted", "done"].includes(d.kind))
                setGhosts((g) => [...g, { name, kind: d.kind, seq }].slice(-4));
              setTimeout(() => setGhosts((g) => g.filter((x) => x.seq !== seq)), 1600);
              setMonActive(true);
              clearTimeout(monTimer.current);
              monTimer.current = setTimeout(() => setMonActive(false), d.kind === "fired" || d.kind === "failed" ? 9000 : 5000);
              if (d.kind === "fired") chime();
            }
          }
          if (ev.type === "notify") {
            const line = `🔔 ${ev.data.title}: ${ev.data.text}`;
            if (ev.request_id) notifiedRuns.current.add(ev.request_id);
            note({ kind: "info", text: line });
            setMessages((m) => [...m, { role: "assistant", text: line, skill: ev.data.origin || "notify" }]);
            notifySystem(ev.data.title, ev.data.text, ev.data.origin);
          }
          if (ev.type === "timer") {
            chime();
            const line = `Your ${ev.data.label || "timer"} timer is done, sir.${ev.data.late ? " It went off while I was restarting." : ""}`;
            setMessages((m) => [...m, { role: "assistant", text: line }]);
            if (speakRef.current) speakRef.current(line);
          }
          if (ev.type === "tool_result") flash(ev.data.status === "ok" || ev.data.status === "dry-run" ? "green" : ev.data.status === "blocked" ? "orange" : "red");
          if (ev.type === "tool_call")
            push({ ts: ev.ts, req: ev.request_id, tool: ev.data.tool, args: ev.data.arguments, risk: ev.data.risk,
              status: "running", source: ev.source });
          if (ev.type === "approval_request") settle(ev.request_id, ev.data.tool, { status: "pending" });
          if (ev.type === "tool_result")
            settle(ev.request_id, ev.data.tool, { status: ev.data.status, preview: ev.data.preview || "", done: ev.ts });
          if (ev.type === "kb_retrieved") settle(ev.request_id, "kb_search", { pages: ev.data.pages });
          // an agent's tool (web, claude, a job) is shown by the orb's constellation: logged, but the console
          // doesn't grow and dock the orb
          const log = AGENT_TOOLS[ev.data?.tool] ? note : echo;
          if (ev.type === "tool_call") log({ kind: "cmd", text: asCommand(ev.data.tool, ev.data.arguments), risk: ev.data.risk });
          if (ev.type === "approval_request") log({ kind: "wait" });
          if (ev.type === "tool_result") {
            const out = (ev.data.preview || "").split("\n").filter((l) => l.trim()).slice(0, 8).join("\n");
            log(...(out ? [{ kind: "out", text: out }] : []), { kind: "exit", status: ev.data.status });
          }
          const kb = kbTouch(ev);
          if (kb) {
            setKbPulse((p) => ({ ...kb, seq: (p?.seq || 0) + 1 }));
            setKbActive(true);
            clearTimeout(kbTimer.current);
            kbTimer.current = setTimeout(() => setKbActive(false), 9000);
            if (kb.kind === "write" && kb.status === "ok") setKbVersion((v) => v + 1);
          }
          if (ev.type === "approval_resolved")
            setApprovals((a) => a.filter((x) => x.approval_id !== ev.data.approval_id));
          if (ev.type === "approval_request") {
            setApprovals((a) => (a.some((x) => x.approval_id === ev.data.approval_id) ? a : [...a, ev.data]));
            setTab("talk"); // phone: an approval card always comes to the front
          }
        };
        ws.onclose = () => {
          setLink("offline");
          if (!stop) retry = setTimeout(connect, 3000);
        };
      } catch {
        setLink("offline");
        if (!stop) retry = setTimeout(connect, 3000);
      }
    };
    connect();
    return () => {
      stop = true;
      clearTimeout(retry);
      clearTimeout(kbTimer.current);
      clearTimeout(monTimer.current);
      clearTimeout(consoleTimer.current);
      ws?.close();
    };
  }, []);

  // Follow the reply as it grows (streaming and the typewriter both add text after the messages
  // change), unless the user has scrolled up to read; sending a question re-attaches.
  const stick = useRef(true);
  useEffect(() => {
    const el = chatRef.current;
    if (!el) return;
    const onScroll = () => { stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48; };
    const follow = new MutationObserver(() => { if (stick.current) el.scrollTop = el.scrollHeight; });
    el.addEventListener("scroll", onScroll, { passive: true });
    follow.observe(el, { childList: true, subtree: true, characterData: true });
    return () => { el.removeEventListener("scroll", onScroll); follow.disconnect(); };
  }, []);

  const send = async (text, spoken = false) => {
    if (!text.trim()) return;
    byVoice.current = spoken;
    const mine = ++turn.current;
    stopAudio(); // a new question cuts the previous answer short
    const attached = context;
    const history = [...messages, { role: "user", text, sent: withContext(text, attached),
      pages: attached.map((p) => p.name) }];
    setContext([]);
    stick.current = true;
    setMessages(history);
    setInput("");
    setState("thinking");
    // The reply streams in: text shows as it arrives and each finished sentence is sent to TTS
    // straight away, so JARVIS starts talking while the rest is still being written.
    const voice = speak ? speaker(mine) : null;
    let shown = "", pending = "", final = null, added = false;
    // the reply's own entry, not just the last one: an alert (a model switch) can land while it streams
    const show = (text) => setMessages((m) => {
      if (!added) { added = true; return [...m, { role: "assistant", text }]; }
      const i = m.findLastIndex((x) => x.role === "assistant");
      return i < 0 ? [...m, { role: "assistant", text }] : [...m.slice(0, i), { role: "assistant", text }, ...m.slice(i + 1)];
    });
    try {
      const r = await api("chat/completions", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Jarvis-Session": sessionId.current },
        body: JSON.stringify({
          model: "jarvis",
          stream: true,
          messages: history.filter((m) => m.role !== "alert" && m.role !== "widget").slice(-12)
            .map((m) => ({ role: m.role, content: m.sent || m.text })),
        }),
      });
      if (!r.ok || !r.body) {
        const body = await r.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${r.status}`);
      }
      for await (const ev of sse(r.body)) {
        if (ev.jarvis?.reset) { // the model talked, then decided to use a tool: drop that
          shown = pending = "";
          voice?.reset();
          continue;
        }
        if (ev.jarvis?.final != null) final = ev.jarvis.final;
        const piece = ev.choices?.[0]?.delta?.content;
        if (!piece) continue;
        shown += piece;
        show(shown);
        if (voice && mine === turn.current) {
          pending += piece;
          let cut;
          while ((cut = sentenceEnd(pending))) {
            voice.add(pending.slice(0, cut));
            pending = pending.slice(cut);
          }
        }
      }
      const reply = final ?? (shown || "No reply.");
      show(reply);
      if (mine !== turn.current) return; // superseded: shown, not spoken
      if (voice) {
        if (!shown.trim()) voice.add(reply); // a tool's own answer arrives whole
        else if (pending.trim()) voice.add(pending);
        await voice.done();
      }
      if (mine === turn.current) {
        setState("idle");
        // asked by voice and answered aloud: keep listening a moment, no button needed ("yes", "and the kitchen")
        if (voice && byVoice.current) followUp(mine);
      }
    } catch (e) {
      if (!added && e?.message) show(e.message);
      if (mine === turn.current) setState("error");
    }
  };

  // Plays sentences in order as their audio arrives; TTS requests run ahead in parallel.
  const speaker = (mine) => {
    let chain = Promise.resolve();
    let gen = 0;
    let budget = 600; // characters: a runaway answer is shown in full but not read out in full
    return {
      add(text) {
        if (budget <= 0 || !text.trim()) return;
        budget -= text.length;
        const g = gen;
        const audio = api("audio/speech", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ input: text }),
        }).then((r) => (r.ok && r.status !== 204 ? r.blob() : null)).catch(() => null);
        chain = chain.then(async () => {
          const blob = await audio;
          if (!blob || g !== gen || mine !== turn.current) return;
          setState("speaking");
          await playAudio(blob, setAnalyser);
        });
      },
      reset() {
        gen++;
        budget = 600;
        stopAudio();
      },
      async done() {
        await chain;
        if (mine === turn.current) setAnalyser(null);
      },
    };
  };

  const say = async (text, mine) => {
    const r = await api("audio/speech", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ input: text }),
    });
    if (!r.ok || r.status === 204 || mine !== turn.current) return; // 204: nothing worth saying aloud
    setState("speaking");
    await playAudio(await r.blob(), setAnalyser);
    if (mine === turn.current) setAnalyser(null);
  };
  speakRef.current = speak ? (text) => {
    const mine = ++turn.current;
    stopAudio();
    say(text, mine).then(() => mine === turn.current && setState("idle"));
  } : null;

  // after a spoken reply, listen for up to 7.5 s; speech then a pause sends it, silence ends it quietly
  const followUp = async (mine) => {
    if (recording.current || !window.isSecureContext) return;
    let rec;
    try { rec = await startRecording(); } catch { return; }
    if (mine !== turn.current) { rec.stop().catch(() => null); return; }
    recording.current = rec;
    setAnalyser(rec.analyser);
    setState("listening");
    const spoke = await heardSpeech(rec.analyser, { cancelled: () => recording.current !== rec });
    if (recording.current !== rec) return; // the mic button or a typed message took over
    if (spoke) return toggleMic(); // stop, transcribe, send
    recording.current = null;
    byVoice.current = false;
    rec.stop().catch(() => null);
    setAnalyser(null);
    setState("idle");
  };

  const toggleMic = async () => {
    if (recording.current) {
      const rec = recording.current;
      recording.current = null;
      setAnalyser(null);
      setState("thinking");
      const wav = await rec.stop();
      const form = new FormData();
      form.append("file", wav, "speech.wav");
      const r = await api("audio/transcriptions", { method: "POST", body: form });
      const { text, detail } = await r.json();
      if (!r.ok || !text) {
        setState("idle");
        setMessages((m) => [...m, { role: "assistant", text: detail || "I didn't catch that, sir." }]);
        return;
      }
      return send(text, true);
    }
    const say_ = (text) => setMessages((m) => [...m, { role: "assistant", text }]);
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      // browsers only allow the mic on https:// or localhost
      say_("I can't hear you on this connection, sir. Browsers only allow the microphone over HTTPS; "
        + "open the https:// address instead, or type.");
      return;
    }
    turn.current++;
    stopAudio();
    try {
      const rec = await startRecording();
      recording.current = rec;
      setAnalyser(rec.analyser);
      setState("listening");
    } catch (e) {
      const denied = e?.name === "NotAllowedError" || e?.name === "SecurityError";
      say_(denied ? "The microphone is blocked for this page, sir. Allow it in the browser's site settings."
        : "I couldn't reach a microphone, sir.");
      setState("idle");
    }
  };

  const switchModel = async (model) => {
    setSwitching(true);
    try {
      const r = await api("llm", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model }) });
      if (r.ok) setStatus((s) => ({ ...s, llm: { ...s?.llm, model } }));
    } finally {
      setSwitching(false);
    }
  };


  const decide = async (id, approved, allowMinutes) => {
    await api(`approvals/${id}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved, ...(allowMinutes ? { allow_minutes: allowMinutes } : {}) }),
    });
    // play the exit animation, then drop the card (instant under reduced motion)
    setLeaving((s) => new Set(s).add(id));
    setTimeout(() => {
      setApprovals((a) => a.filter((x) => x.approval_id !== id));
      setLeaving((s) => {
        const n = new Set(s);
        n.delete(id);
        return n;
      });
    }, reducedMotion() ? 0 : 300);
  };

  return (
    <>
    {bootPhase !== "off" && (
      <BootScreen leaving={bootPhase === "leaving"} steps={[
        { label: "Hub uplink", state: boot.hub },
        { label: "Event stream", state: boot.events },
        { label: "Action log", state: boot.audit },
        { label: "Knowledge graph", state: boot.graph },
      ]} />
    )}
    <Toasts feed={feed} />
    {kbView === "max" && <KbExplorer version={kbVersion} context={context} onAdd={addContext} onClose={closeKb} />}
    <div className={`hud tab-${tab}${shown.focus ? ` docked focus-${shown.focus}` : ""}`}>
      <header className="reveal" style={{ "--i": 0 }}>
        <h1 className={link === "online" ? "" : "offline"} title={`hub link ${link}`}>J.A.R.V.I.S.</h1>
        <div className="chips">
          <button type="button" className="chip new-session" onClick={newSession}
            title="Clear this conversation and start fresh (JARVIS forgets the chat; memory pages stay)">⟲ new session</button>
          <Health />
          {models.length > 1 && (
            <label className={`chip model-pick${switching ? " busy" : ""}`}
              title={`Language model${llmInfo?.loaded === false ? " · not loaded: the next reply waits ~13 s"
                : llmInfo?.loaded ? " · loaded" : ""}${llmInfo?.median_s ? ` · replies take about ${llmInfo.median_s} s` : ""}`}>
              {llmInfo?.loaded != null && <span className={`warm ${llmInfo.loaded ? "on" : ""}`} aria-hidden="true" />}
              llm{llmInfo?.median_s ? <small> {llmInfo.median_s}s</small> : null}
              <select value={status?.llm?.model || ""} disabled={switching} aria-label="Language model"
                onChange={(e) => switchModel(e.target.value)}>
                {models.map((m) => <option key={m} value={m}>{modelLabel(m)}</option>)}
              </select>
            </label>
          )}
          {status && (
            <div className="tools-pick">
              <button type="button" className="chip" aria-expanded={skillOpen}
                onClick={() => openOnly(skillOpen ? null : "skill")}
                title="Skills: knowledge-base pages that run on events">
                skills{skillCount != null ? ` ${skillCount}` : ""} {skillOpen ? "▴" : "▾"}
              </button>
              {skillOpen && (
                <div className="tools-list" role="dialog" aria-label="Skills">
                  <SkillsPanel onCount={setSkillCount} version={hookSeq} />
                </div>
              )}
            </div>
          )}
          {status && (
            <div className="tools-pick">
              <button type="button" className="chip" aria-expanded={watchOpen}
                onClick={() => openOnly(watchOpen ? null : "watch")}
                title="Watchers: checks that notify you of new PRs, pipelines, mail…">
                watch{monitors?.watchers?.length ? ` ${monitors.watchers.filter((w) => w.enabled).length}` : ""} {watchOpen ? "▴" : "▾"}
              </button>
              {watchOpen && (
                <div className="tools-list wide" role="dialog" aria-label="Watchers">
                  <WatchPanel data={monitors} onChange={loadMonitors} />
                </div>
              )}
            </div>
          )}
          {status && (
            <div className="tools-pick">
              <button type="button" className="chip" aria-expanded={settingsOpen}
                onClick={() => openOnly(settingsOpen ? null : "settings")}
                title="Settings: webhooks, guard (what JARVIS may do unasked), devices, tools">
                ⚙ {settingsOpen ? "▴" : "▾"}
              </button>
              {settingsOpen && (
                <div className="tools-list wide" role="dialog" aria-label="Settings">
                  <div className="set-tabs" role="tablist">
                    {[["webhooks", "webhooks"], ["guard", "guard"], ["dev", `devices${devCount != null ? ` ${devCount}` : ""}`],
                      ["wit", "wit"], ["tools", `tools ${status.tools?.length ?? ""}`]].map(([id, label]) => (
                      <button key={id} type="button" role="tab" aria-selected={settingsTab === id}
                        className={settingsTab === id ? "on" : ""} onClick={() => setSettingsTab(id)}>{label}</button>
                    ))}
                  </div>
                  {settingsTab === "webhooks" && <WebhooksPanel version={hookSeq} />}
                  {settingsTab === "wit" && <WitPanel />}
                  {settingsTab === "guard" && <GuardPanel version={hookSeq} />}
                  {settingsTab === "dev" && <DevicesPanel onCount={setDevCount} version={hookSeq} />}
                  {settingsTab === "tools" && (
                    <>
                      <p className="tools-note">auto = runs straight away (audited) · ask = waits for your approval</p>
                      <ul>
                        {[...(status.tools || [])].sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" })).map((t) => (
                          <li key={t.name} title={t.description || ""}>
                            <span className={`gate ${t.asks ? "ask" : "auto"}`}>{t.asks ? "ask" : "auto"}</span>
                            <b>{t.name}</b>
                            <i>{t.origin} · {t.risk}</i>
                            {t.description && <small>{t.description}</small>}
                          </li>
                        ))}
                      </ul>
                    </>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </header>

      <div className="side left reveal" style={{ "--i": 2 }}>
      <section className={`panel timeline${shown.focus === "timeline" ? " focused" : ""}`}>
        <h2 {...headProps(pinned === "timeline", () => togglePin("timeline"), "action timeline")}>
          Action timeline<b className="expand" aria-hidden="true" />
        </h2>
        <ul className="tl-list">
          {timeline.map((e, i) => {
            const d = describe(e.tool, e.args);
            const write = e.risk && e.risk !== "read";
            const title = [fmtTime(e.ts), e.tool, e.pages?.length ? `recalled: ${e.pages.join(", ")}` : "",
              e.by && !String(e.by).startsWith("policy") ? `approved by ${e.by}` : ""].filter(Boolean).join(" · ");
            return (
              <li key={e.id} className={`tl ${e.status} ${write ? "write" : "read"}`} style={{ "--i": Math.min(i, 12) }} title={title}>
                <span className="tl-glyph" aria-hidden="true">{d.glyph}</span>
                <div className="tl-main">
                  <div className="tl-title">
                    {d.title}
                    {e.source && e.source !== "web" && <span className="tl-src">{SOURCE[e.source] || e.source}</span>}
                  </div>
                  {d.detail && <div className="tl-detail">{d.detail}</div>}
                </div>
                <div className="tl-meta">
                  <span className="tl-insight">{insight(e.tool, e.status, e.preview)}</span>
                  <time dateTime={new Date(e.ts * 1000).toISOString()}>{ago(e.ts, now)}</time>
                </div>
              </li>
            );
          })}
          {!timeline.length && <li className="empty">Standing by. No actions yet.</li>}
        </ul>
      </section>
      <SystemConsole lines={consoleLines} active={shown.con} focused={shown.focus === "console"}
        pinned={pinned === "console"} onToggle={() => togglePin("console")} />
      <LiveNote feed={feed} pinned={pinned === "notes"} focused={shown.focus === "notes"}
        head={headProps(pinned === "notes", () => togglePin("notes"), "notes being written")} />
      </div>

      <section className="core reveal" style={{ "--i": 1 }}>
        <div className={`hero${orbAside ? " has-job" : ""}`}>
          <div className="orb-wrap" {...(pinned ? { role: "button", tabIndex: 0, title: "Back to JARVIS (Esc)",
            onClick: () => setPinned(null), onKeyDown: (e) => e.key === "Enter" && setPinned(null) } : {})}>
            <Orb state={state} analyser={analyser} accent={accent} alert={approvals.length > 0} />
            <Constellation feed={feed} onActive={setAgentsOn} />
          </div>
          <div className="state" data-state={state}>
            <Decode label={state.toUpperCase()} />
            <span className="sr-only">{state}</span>
            {approvals.length > 0
              ? <span className="state-sub alert">awaiting your approval</span>
              : shown.focus && <span className="state-sub">{pinned ? "pinned · esc to return" : `in ${{ kb: "knowledge map", timeline: "timeline", watch: "monitors" }[shown.focus] || "console"}`}</span>}
          </div>
          {jobCards.length > 0 && (
            <div className={`job-stack${jobCards.length > 1 ? " many" : ""}`} aria-label="Background jobs">
              {jobCards.map((j) => (
                <JobBox key={j.id} job={j} closing={closingJobs.has(j.id)} onDismiss={dismissJob} onCancel={cancelJob} />
              ))}
            </div>
          )}
        </div>
        <DreamDigest />
        <div className="approvals">
          {approvals.map((a) => {
            const out = leaving.has(a.approval_id);
            return (
              <div key={a.approval_id} className={`approval ${a.risk}${out ? " leaving" : ""}`}
                role="group" aria-label={`Approval required for ${a.tool}`}>
                <div className="title">APPROVAL REQUIRED · {a.risk.toUpperCase()}</div>
                <div className="tool">{a.says ? <>{a.says.charAt(0).toUpperCase() + a.says.slice(1)}</> : a.tool}</div>
                {a.reason && <div className="reason">⚠ Held because it {a.reason}.</div>}
                <Preview p={a.preview} />
                <details>
                  <summary>{a.tool} · details</summary>
                  <pre>{JSON.stringify(a.arguments, null, 1)}</pre>
                </details>
                <div className="meta">
                  requested via {a.requested_by}
                  {a.orphan ? " · left over from before a restart: approving runs it now"
                    : a.expires ? ` · ${Math.max(0, Math.round(a.expires - now))} s left` : ""}
                </div>
                <div className="buttons">
                  <button onClick={() => decide(a.approval_id, true)} disabled={out}>Approve</button>
                  {!a.reason && !a.orphan && (
                    <button className="grant" onClick={() => decide(a.approval_id, true, 60)} disabled={out}
                      title={`Approve, and let ${a.tool} run without asking for the next hour`}>Allow 1 h</button>
                  )}
                  <button className="deny" onClick={() => decide(a.approval_id, false)} disabled={out}>Deny</button>
                </div>
              </div>
            );
          })}
        </div>
        <div className="chat" ref={chatRef}>
          {messages.map((m, i) => (m.role === "widget"
            ? <Widget key={i} spec={m.spec} />
            : <Message key={i} m={m} latest={i === messages.findLastIndex((x) => x.role !== "widget")} />))}
        </div>
        {/* the beam animates its own wrapper, so the entrance animation sits on the form */}
        {context.length > 0 && (
          <div className="ctx-chips" aria-label="Pages attached to your next question">
            {context.map((c) => (
              <span key={c.name} className="ctx-chip">◈ {c.name}
                <button type="button" aria-label={`Remove ${c.name}`}
                  onClick={() => setContext((x) => x.filter((y) => y !== c))}>×</button>
              </span>
            ))}
          </div>
        )}
        <BorderBeam className="input-wrap" size="md" colorVariant="colorful" strength={0.7}
          theme="dark" duration={state === "thinking" ? 1 : 1.96}>
        <form className={`input reveal${dropping ? " dropping" : ""}`} style={{ "--i": 4 }}
          onSubmit={(e) => { e.preventDefault(); send(input); }}
          onDragOver={(e) => { if (e.dataTransfer.types.includes(PAGE_MIME)) { e.preventDefault(); setDropping(true); } }}
          onDragLeave={() => setDropping(false)} onDrop={dropPage}>
          <button type="button" className={`mic ${state === "listening" ? "on" : ""}`} onClick={toggleMic}
            aria-label={state === "listening" ? "Stop and send" : "Speak"} aria-pressed={state === "listening"}>
            {state === "listening" ? <StopIcon /> : <MicIcon />}
          </button>
          <textarea value={input} rows={1} placeholder="Speak or type, sir…" title="Enter sends · Shift+Enter: new line"
            aria-label="Message JARVIS" autoComplete="off"
            onChange={(e) => {
              setInput(e.target.value);
              const t = e.target;
              t.style.height = "auto";
              t.style.height = `${Math.min(t.scrollHeight, 160)}px`;
              t.style.overflowY = t.scrollHeight > 160 ? "auto" : "hidden";
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                send(input);
                e.currentTarget.style.height = "auto";
              }
            }} />
          <button type="submit" className="send">Send</button>
          <label className="speak"><input type="checkbox" checked={speak} onChange={(e) => setSpeak(e.target.checked)} /> voice</label>
        </form>
        </BorderBeam>
      </section>

      <div className="side map-dock reveal" style={{ "--i": 3 }}>
      <MonitorsPanel data={monitors} pulse={monPulse} ghosts={ghosts} active={shown.mon} focused={shown.focus === "watch"}
        headProps={headProps(pinned === "watch", () => togglePin("watch"), "monitors")}
        onManage={(e) => { e?.stopPropagation?.(); openOnly("watch"); }}
        onClearJobs={async () => { await api("jobs/clear", { method: "POST" }).catch(() => null); loadMonitors(); }} />
      <section className={`panel kb minimap${shown.kb ? ` active k-${kbPulse?.kind || "read"}` : ""}${shown.focus === "kb" ? " focused" : ""}${kbView === "min" ? " minimized" : ""}`}
        aria-label="Knowledge map">
        <i className="mm-corner tl" aria-hidden="true" /><i className="mm-corner tr" aria-hidden="true" />
        <i className="mm-corner bl" aria-hidden="true" /><i className="mm-corner br" aria-hidden="true" />
        <div className="kb-head">
          <h2 {...headProps(pinned === "kb", () => togglePin("kb"), "knowledge map")}>
            Knowledge map
            <span className="kb-state">
              {kbActive ? ({ read: "recall", tasks: "tasks", write: "writing", erase: "erasing", fault: "fault" }[kbPulse?.kind] || "recall")
                : kbStats ? `${kbStats.pages} pages` : ""}
              <b className="expand" aria-hidden="true" />
            </span>
          </h2>
          <div className="win-btns">
            <button type="button" onClick={() => setKbView((v) => (v === "min" ? "normal" : "min"))}
              title={kbView === "min" ? "Restore" : "Minimise"} aria-label={kbView === "min" ? "Restore knowledge map" : "Minimise knowledge map"}>
              {kbView === "min" ? "▢" : "–"}
            </button>
            <button type="button" onClick={() => setKbView("max")} title="Explore: read pages, add them to chat"
              aria-label="Open the knowledge graph explorer">⤢</button>
          </div>
        </div>
        {kbView === "normal" && (
          <div className="mm-view">
            <KbGraph pulse={kbPulse} active={kbActive} version={kbVersion} onStats={setKbStats} />
            {kbActive && <div key={kbPulse?.seq} className="mm-ping" aria-hidden="true" />}
          </div>
        )}
        {kbActive && (
          <div className="kb-touched" aria-live="polite">
            {kbPulse?.label && <b>{kbPulse.label}</b>}
            {kbPulse?.pages?.length > 0 && <span>{[...new Set(kbPulse.pages)].slice(0, 4).join(" · ")}</span>}
          </div>
        )}
      </section>

      </div>

      <nav className="mnav" aria-label="Sections">
        {[
          ["talk", "◉", "Talk", approvals.length > 0],
          ["activity", "⟐", "Activity", consoleActive],
          ["watch", "◴", "Monitors", monActive || jobCards.some((j) => j.state === "running" || j.state === "queued")],
          ["map", "◈", "Knowledge", kbActive],
        ].map(([id, glyph, label, live]) => (
          <button key={id} type="button" className={`${tab === id ? "on" : ""} ${live && tab !== id ? "live" : ""}`}
            aria-current={tab === id ? "page" : undefined} onClick={() => setTab(id)}>
            <span aria-hidden="true">{glyph}</span>{label}
          </button>
        ))}
      </nav>
    </div>
    </>
  );
}

// "20261103-151204-k3f9": when the session started, readable in the hub's traces, plus a few random letters
function newSessionId() {
  const d = new Date(), p = (n) => String(n).padStart(2, "0");
  const when = `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
  return `${when}-${Math.random().toString(36).slice(2, 6)}`;
}
