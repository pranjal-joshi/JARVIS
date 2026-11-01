"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";
import Orb from "./Orb";
import SystemConsole, { asCommand } from "./SystemConsole";
import { SOURCE, ago, describe, insight } from "../lib/timeline";
import { playAudio, startRecording } from "../lib/audio";

// WebGL graph: client-only so the server build never touches three.js
const KbGraph = dynamic(() => import("./KbGraph"), {
  ssr: false,
  loading: () => <div className="kb-canvas kb-loading">initialising graph…</div>,
});

const api = (path, init) => fetch(`/api/hub/${path}`, { cache: "no-store", ...init });

function useTypewriter(text, speed = 14) {
  const [shown, setShown] = useState("");
  useEffect(() => {
    if (!text) {
      setShown("");
      return;
    }
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setShown(text);
      return;
    }
    let i = 0;
    const id = setInterval(() => {
      i += 2;
      setShown(text.slice(0, i));
      if (i >= text.length) clearInterval(id);
    }, speed);
    return () => clearInterval(id);
  }, [text, speed]);
  return shown;
}

const reducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// Short "decode" reveal: random glyphs resolve left-to-right into the label.
const GLYPHS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789#%/+=";
function useDecode(label, duration = 420) {
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
  return shown;
}

// Which KB pages an event touched, and whether it was a read or a write.
const KB_WRITES = new Set(["kb_remember", "kb_journal", "kb_append_to_page"]);
function kbTouch(ev) {
  if (ev.type === "kb_retrieved") return { pages: ev.data?.pages || [], kind: "read", tool: "kb_search" };
  const tool = ev.data?.tool;
  if ((ev.type !== "tool_call" && ev.type !== "tool_result") || !tool?.startsWith("kb_")) return null;
  const kind = KB_WRITES.has(tool) ? "write" : "read";
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
  return { pages, kind, tool, status: ev.type === "tool_result" ? ev.data.status : null };
}

// Stable keys so newly pushed timeline rows animate in (index keys would not).
let seq = 0;
const withId = (item) => ({ ...item, id: ++seq });

function Message({ m, latest }) {
  const typed = useTypewriter(latest && m.role === "assistant" ? m.text : "");
  const text = latest && m.role === "assistant" ? typed : m.text;
  return (
    <div className={`msg ${m.role}`}>
      <span className="who">{m.role === "user" ? "YOU" : "JARVIS"}</span>
      <span className="text">{text}</span>
    </div>
  );
}

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export default function Hud() {
  const [state, setState] = useState("idle");
  const [analyser, setAnalyser] = useState(null);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [timeline, setTimeline] = useState([]);
  const [approvals, setApprovals] = useState([]);
  const [memory, setMemory] = useState([]);
  const [status, setStatus] = useState(null);
  const [link, setLink] = useState("connecting");
  const [speak, setSpeak] = useState(true);
  const [leaving, setLeaving] = useState(() => new Set());
  const [kbPulse, setKbPulse] = useState(null);
  const [kbActive, setKbActive] = useState(false);
  const [kbVersion, setKbVersion] = useState(0);
  const [kbStats, setKbStats] = useState(null);
  const kbTimer = useRef(null);
  const [consoleLines, setConsoleLines] = useState([]);
  const [consoleActive, setConsoleActive] = useState(false);
  const [accent, setAccent] = useState(null); // orb colour flash: { color, at }
  const flash = (color) => setAccent({ color, at: Date.now() });
  const consoleTimer = useRef(null);
  const recording = useRef(null);
  const chatEnd = useRef(null);
  const stateLabel = useDecode(state.toUpperCase());

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
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now() / 1000), 15000);
    return () => clearInterval(id);
  }, []);

  // console: append lines, light the window up, let it settle after a few quiet seconds
  const echo = (...lines) => {
    setConsoleLines((c) => [...c, ...lines.map(withId)].slice(-120));
    setConsoleActive(true);
    clearTimeout(consoleTimer.current);
    consoleTimer.current = setTimeout(() => setConsoleActive(false), 6000);
  };

  const refreshMemory = useCallback(async () => {
    const r = await api("kb/memory?limit=15");
    if (r.ok) setMemory(await r.json());
  }, []);

  // initial data
  useEffect(() => {
    (async () => {
      const [a, s, p] = await Promise.all([api("audit?limit=30"), api("status"), api("approvals")]);
      if (a.ok) {
        const rows = await a.json();
        const parse = (s) => { try { return JSON.parse(s || "{}"); } catch { return {}; } };
        setTimeline(rows.map((r) => withId({ ts: r.ts, req: r.request_id, tool: r.tool, args: parse(r.arguments),
          risk: r.risk, status: r.status, preview: r.result || "", source: r.source, by: r.approved_by })));
      }
      if (s.ok) setStatus(await s.json());
      if (p.ok) setApprovals(await p.json());
      refreshMemory();
    })();
  }, [refreshMemory]);

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
          if (ev.type === "thinking") setState("thinking");
          if (ev.type === "tool_call" && ev.data.risk && ev.data.risk !== "read") flash("orange");
          if (ev.type === "approval_request") flash("orange");
          if (ev.type === "tool_result") flash(ev.data.status === "ok" ? "green" : "red");
          if (ev.type === "tool_call")
            push({ ts: ev.ts, req: ev.request_id, tool: ev.data.tool, args: ev.data.arguments, risk: ev.data.risk,
              status: "running", source: ev.source });
          if (ev.type === "approval_request") settle(ev.request_id, ev.data.tool, { status: "pending" });
          if (ev.type === "tool_result")
            settle(ev.request_id, ev.data.tool, { status: ev.data.status, preview: ev.data.preview || "", done: ev.ts });
          if (ev.type === "kb_retrieved") settle(ev.request_id, "kb_search", { pages: ev.data.pages });
          if (ev.type === "tool_call") echo({ kind: "cmd", text: asCommand(ev.data.tool, ev.data.arguments), risk: ev.data.risk });
          if (ev.type === "approval_request") echo({ kind: "wait" });
          if (ev.type === "tool_result") {
            const out = (ev.data.preview || "").split("\n").filter((l) => l.trim()).slice(0, 8).join("\n");
            echo(...(out ? [{ kind: "out", text: out }] : []), { kind: "exit", status: ev.data.status });
          }
          const kb = kbTouch(ev);
          if (kb) {
            setKbPulse((p) => ({ ...kb, seq: (p?.seq || 0) + 1 }));
            setKbActive(true);
            clearTimeout(kbTimer.current);
            kbTimer.current = setTimeout(() => setKbActive(false), 9000);
            if (kb.kind === "write" && kb.status === "ok") setKbVersion((v) => v + 1);
          }
          if (ev.type === "approval_request")
            setApprovals((a) => (a.some((x) => x.approval_id === ev.data.approval_id) ? a : [...a, ev.data]));
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
      clearTimeout(consoleTimer.current);
      ws?.close();
    };
  }, []);

  useEffect(() => {
    chatEnd.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const send = async (text) => {
    if (!text.trim()) return;
    const history = [...messages, { role: "user", text }];
    setMessages(history);
    setInput("");
    setState("thinking");
    try {
      const r = await api("chat/completions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: "jarvis",
          messages: history.slice(-12).map((m) => ({ role: m.role, content: m.text })),
        }),
      });
      const body = await r.json();
      const reply = body.choices?.[0]?.message?.content || body.detail || "No reply.";
      setMessages((m) => [...m, { role: "assistant", text: reply }]);
      refreshMemory();
      if (speak) await say(reply);
      setState("idle");
    } catch {
      setState("error");
    }
  };

  const say = async (text) => {
    const r = await api("audio/speech", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ input: text }),
    });
    if (!r.ok) return;
    setState("speaking");
    await playAudio(await r.blob(), setAnalyser);
    setAnalyser(null);
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
      return send(text);
    }
    try {
      const rec = await startRecording();
      recording.current = rec;
      setAnalyser(rec.analyser);
      setState("listening");
    } catch {
      setState("error");
    }
  };

  const decide = async (id, approved) => {
    await api(`approvals/${id}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved }),
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
    <div className="hud">
      <header className="reveal" style={{ "--i": 0 }}>
        <h1>J.A.R.V.I.S.</h1>
        <div className="chips">
          <span className={`chip ${link}`}>link {link}</span>
          {status && <span className="chip">llm {status.llm?.model} {status.llm?.up ? "●" : "○"}</span>}
          {status && <span className="chip">kb {status.kb?.vector ? "vector" : "keyword"}</span>}
          {status && <span className="chip">tts {status.voice?.voicestudio === "up" ? "jarvis" : "piper"}</span>}
          {status && <span className="chip">tools {status.tools?.length}</span>}
        </div>
      </header>

      <div className="side left reveal" style={{ "--i": 2 }}>
      <section className="panel timeline">
        <h2>Action timeline</h2>
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
      <SystemConsole lines={consoleLines} active={consoleActive} />
      </div>

      <section className="core reveal" style={{ "--i": 1 }}>
        <div className="orb-wrap">
          <Orb state={state} analyser={analyser} accent={accent} />
        </div>
        <div className="state" data-state={state}>
          <span aria-hidden="true">{stateLabel}</span>
          <span className="sr-only">{state}</span>
        </div>
        <div className="approvals">
          {approvals.map((a) => {
            const out = leaving.has(a.approval_id);
            return (
              <div key={a.approval_id} className={`approval ${a.risk}${out ? " leaving" : ""}`}
                role="group" aria-label={`Approval required for ${a.tool}`}>
                <div className="title">APPROVAL REQUIRED · {a.risk.toUpperCase()}</div>
                <div className="tool">{a.tool}</div>
                <pre>{JSON.stringify(a.arguments, null, 1)}</pre>
                <div className="meta">requested via {a.requested_by}</div>
                <div className="buttons">
                  <button onClick={() => decide(a.approval_id, true)} disabled={out}>Approve</button>
                  <button className="deny" onClick={() => decide(a.approval_id, false)} disabled={out}>Deny</button>
                </div>
              </div>
            );
          })}
        </div>
        <div className="chat">
          {messages.map((m, i) => <Message key={i} m={m} latest={i === messages.length - 1} />)}
          <div ref={chatEnd} />
        </div>
        <form className="input reveal" style={{ "--i": 4 }} onSubmit={(e) => { e.preventDefault(); send(input); }}>
          <button type="button" className={`mic ${state === "listening" ? "on" : ""}`} onClick={toggleMic}
            aria-label={state === "listening" ? "Stop and send" : "Speak"} aria-pressed={state === "listening"}>
            {state === "listening" ? "■" : "◉"}
          </button>
          <input value={input} onChange={(e) => setInput(e.target.value)} placeholder="Speak or type, sir…"
            aria-label="Message JARVIS" autoComplete="off" />
          <button type="submit" className="send">Send</button>
          <label className="speak"><input type="checkbox" checked={speak} onChange={(e) => setSpeak(e.target.checked)} /> voice</label>
        </form>
      </section>

      <div className="side reveal" style={{ "--i": 3 }}>
      <section className={`panel kb ${kbActive ? "active" : ""} ${kbPulse?.kind === "write" && kbActive ? "write" : ""}`}
        aria-label="Knowledge graph">
        <h2>
          Knowledge graph
          <span className="kb-state">
            {kbActive ? (kbPulse?.kind === "write" ? "writing" : "recall") : kbStats ? `${kbStats.pages} pages` : ""}
          </span>
        </h2>
        <KbGraph pulse={kbPulse} active={kbActive} version={kbVersion} onStats={setKbStats} />
        {kbActive && kbPulse?.pages?.length > 0 && (
          <div className="kb-touched" aria-live="polite">
            {kbPulse.kind === "write" ? "✎ " : "◈ "}{[...new Set(kbPulse.pages)].slice(0, 4).join(" · ")}
          </div>
        )}
      </section>

      <section className="panel memory">
        <h2>JARVIS memory</h2>
        <ul>
          {memory.map((m, i) => (
            <li key={i} style={{ "--i": Math.min(i, 12) }}>
              <a href={m.link} title="Open in Logseq">{m.page}</a>
              <p>{m.text}</p>
            </li>
          ))}
          {!memory.length && <li className="empty">Nothing remembered yet.</li>}
        </ul>
      </section>
      </div>
    </div>
  );
}
