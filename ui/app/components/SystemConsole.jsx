"use client";

// Terminal-style view of what JARVIS runs: every tool call is shown as a command line,
// followed by its output preview and an exit status. Built only from hub events.
import { useEffect, useRef } from "react";
import { headProps } from "../lib/panel";

const quote = (v) => {
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return /^[\w./:@-]+$/.test(s) ? s : `"${s.replace(/(["\\])/g, "\\$1")}"`;
};

export const asCommand = (tool, args) =>
  [tool, ...Object.entries(args || {}).map(([k, v]) => `--${k.replace(/_/g, "-")} ${quote(v)}`)].join(" ");

const STATUS = {
  ok: { code: 0, cls: "ok" },
  error: { code: 1, cls: "err" },
  denied: { code: 126, cls: "err", note: "denied by user" },
  timeout: { code: 124, cls: "err", note: "approval timed out" },
};

export default function SystemConsole({ lines, active, focused, pinned, onToggle }) {
  const body = useRef(null);

  // keep the newest line in view without scrolling the page
  useEffect(() => {
    const el = body.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines]);

  return (
    <section className={`panel console ${active ? "active" : ""}${focused ? " focused" : ""}`} aria-label="System console">
      <h2 {...headProps(pinned, onToggle, "system console")}>
        System console
        <span className="console-dots" aria-hidden="true"><i /><i /><i /><b className="expand" /></span>
      </h2>
      <div className="console-body" ref={body} role="log" aria-live="polite">
        {lines.map((l) => {
          if (l.kind === "cmd")
            return (
              <div key={l.id} className={`cl cmd ${l.risk || ""}`}>
                <span className="prompt">jarvis@hub<b>:</b>~$</span> {l.text}
                {l.risk && l.risk !== "read" && <span className="risk">[{l.risk}]</span>}
              </div>
            );
          if (l.kind === "wait")
            return <div key={l.id} className="cl wait">… waiting for approval in the HUD</div>;
          if (l.kind === "out")
            return <pre key={l.id} className="cl out">{l.text}</pre>;
          if (l.kind === "info") // hook events and skill runs
            return <div key={l.id} className="cl info">{l.text}</div>;
          const st = STATUS[l.status] || { code: "?", cls: "err" };
          return (
            <div key={l.id} className={`cl exit ${st.cls}`}>
              exit {st.code}{st.note ? ` · ${st.note}` : ""}
            </div>
          );
        })}
        {!lines.length && <div className="cl idle">No commands yet. Ask JARVIS to look something up.</div>}
        <div className="cl cursor" aria-hidden="true"><span className="prompt">jarvis@hub<b>:</b>~$</span> <i /></div>
      </div>
    </section>
  );
}
