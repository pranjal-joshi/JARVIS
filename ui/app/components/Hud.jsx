"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Orb from "./Orb";
import { playAudio, startRecording } from "../lib/audio";

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
  const recording = useRef(null);
  const chatEnd = useRef(null);

  const push = (item) => setTimeline((t) => [item, ...t].slice(0, 80));

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
        setTimeline(rows.map((r) => ({ kind: "audit", ts: r.ts, tool: r.tool, risk: r.risk,
          status: r.status, source: r.source, by: r.approved_by })));
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
          if (ev.type === "tool_call") push({ kind: "call", ts: ev.ts, tool: ev.data.tool, risk: ev.data.risk, args: ev.data.arguments });
          if (ev.type === "tool_result") push({ kind: "result", ts: ev.ts, tool: ev.data.tool, status: ev.data.status, preview: ev.data.preview });
          if (ev.type === "kb_retrieved") push({ kind: "kb", ts: ev.ts, pages: ev.data.pages });
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
    setApprovals((a) => a.filter((x) => x.approval_id !== id));
  };

  return (
    <div className="hud">
      <header>
        <h1>J.A.R.V.I.S.</h1>
        <div className="chips">
          <span className={`chip ${link}`}>link {link}</span>
          {status && <span className="chip">llm {status.llm?.model} {status.llm?.up ? "●" : "○"}</span>}
          {status && <span className="chip">kb {status.kb?.vector ? "vector" : "keyword"}</span>}
          {status && <span className="chip">tts {status.voice?.voicestudio === "up" ? "jarvis" : "piper"}</span>}
          {status && <span className="chip">tools {status.tools?.length}</span>}
        </div>
      </header>

      <section className="panel timeline">
        <h2>Action timeline</h2>
        <ul>
          {timeline.map((e, i) => (
            <li key={i} className={`ev ${e.kind} ${e.risk || ""} ${e.status || ""}`}>
              <time>{fmtTime(e.ts)}</time>
              {e.kind === "kb" && <span>◈ recalled {e.pages.join(", ") || "nothing"}</span>}
              {e.kind === "call" && <span>→ {e.tool} <em>{e.risk}</em></span>}
              {e.kind === "result" && <span>← {e.tool} <em>{e.status}</em></span>}
              {e.kind === "audit" && <span>{e.tool} <em>{e.status}</em> · {e.source}{e.by && e.by !== "policy" ? ` · ✓${e.by}` : ""}</span>}
            </li>
          ))}
          {!timeline.length && <li className="empty">No actions yet.</li>}
        </ul>
      </section>

      <section className="core">
        <Orb state={state} analyser={analyser} />
        <div className="state">{state.toUpperCase()}</div>
        {approvals.map((a) => (
          <div key={a.approval_id} className={`approval ${a.risk}`}>
            <div className="title">APPROVAL REQUIRED · {a.risk.toUpperCase()}</div>
            <div className="tool">{a.tool}</div>
            <pre>{JSON.stringify(a.arguments, null, 1)}</pre>
            <div className="meta">requested via {a.requested_by}</div>
            <div className="buttons">
              <button onClick={() => decide(a.approval_id, true)}>Approve</button>
              <button className="deny" onClick={() => decide(a.approval_id, false)}>Deny</button>
            </div>
          </div>
        ))}
        <div className="chat">
          {messages.map((m, i) => <Message key={i} m={m} latest={i === messages.length - 1} />)}
          <div ref={chatEnd} />
        </div>
        <form className="input" onSubmit={(e) => { e.preventDefault(); send(input); }}>
          <button type="button" className={`mic ${state === "listening" ? "on" : ""}`} onClick={toggleMic}
            aria-label={state === "listening" ? "Stop and send" : "Speak"}>
            {state === "listening" ? "■" : "◉"}
          </button>
          <input value={input} onChange={(e) => setInput(e.target.value)} placeholder="Speak or type, sir…" />
          <button type="submit">Send</button>
          <label className="speak"><input type="checkbox" checked={speak} onChange={(e) => setSpeak(e.target.checked)} /> voice</label>
        </form>
      </section>

      <section className="panel memory">
        <h2>JARVIS memory</h2>
        <ul>
          {memory.map((m, i) => (
            <li key={i}>
              <a href={m.link} title="Open in Logseq">{m.page}</a>
              <p>{m.text}</p>
            </li>
          ))}
          {!memory.length && <li className="empty">Nothing remembered yet.</li>}
        </ul>
      </section>
    </div>
  );
}
