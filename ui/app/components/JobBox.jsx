"use client";

// One background job, beside the orb: its goal, how long it has run, and its steps (the job's own to-do list
// when it keeps one, else the last steps it took). A running card can be stopped; once the job ends the card
// shows the outcome and stays until it is dismissed. Several run at once as a stack (Hud.jsx).

import { useEffect, useState } from "react";

const left = (s) => {
  s = Math.max(0, Math.round(s));
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  if (s >= 60) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  return `${s}s`;
};
const MARK = { completed: "✓", in_progress: "◉", pending: "○" };

export default function JobBox({ job, closing, onDismiss, onCancel }) {
  const live = job.state === "running" || job.state === "queued";
  const [now, setNow] = useState(() => Date.now() / 1000);
  const [stopping, setStopping] = useState(false);
  useEffect(() => {
    if (!live) return undefined;
    const id = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(id);
  }, [live]);
  const end = live ? now : job.ended || now;
  const ran = end - (job.started || job.created || end);
  const todos = job.todos || [];
  const trail = (job.trail || []).slice(-5);
  const outcome = job.state === "done" ? "done" : job.state === "cancelled" ? "cancelled" : "failed";

  return (
    <aside className={`job-box ${live ? "live" : `ended ${outcome}`}${closing ? " closing" : ""}`}
      aria-label={`Background job: ${job.goal}`}>
      <div className="job-head">
        <span className="job-kind">{live && <i className="job-pulse" aria-hidden="true" />}
          {live ? "RUNNING" : outcome.toUpperCase()}{job.skill ? ` · ${job.skill}` : ""}</span>
        <span className="job-time">{left(ran)} · {job.steps || 0} steps</span>
        {live ? (
          <button type="button" className="job-x" title="Stop this job" aria-label="Stop this job"
            disabled={stopping} onClick={() => { setStopping(true); onCancel(job.id); }}>■</button>
        ) : (
          <button type="button" className="job-x" title="Dismiss (the report stays in your notes)"
            aria-label="Dismiss" onClick={() => onDismiss(job.id)}>×</button>
        )}
      </div>
      <div className="job-goal" title={job.goal}>{job.goal}</div>
      {live ? (
        todos.length > 0 ? (
          <ol className="job-steps todos">
            {todos.map((t, i) => (
              <li key={`${i}-${t.text}`} className={t.status}>
                <span className="mark" aria-hidden="true">{MARK[t.status] || "○"}</span>
                <span className="text">{t.text}</span>
              </li>
            ))}
          </ol>
        ) : (
          <ol className="job-steps trail">
            {trail.length === 0 && <li className="in_progress"><span className="mark">◉</span>
              <span className="text">{stopping ? "stopping…" : job.last || "starting…"}</span></li>}
            {trail.map((s, i) => (
              <li key={`${s.t}-${i}`} className={i === trail.length - 1 ? "in_progress" : "completed"}>
                <span className="mark" aria-hidden="true">{i === trail.length - 1 ? "◉" : "✓"}</span>
                <span className="text">{s.text}</span>
              </li>
            ))}
          </ol>
        )
      ) : (
        <div className="job-outcome">
          <span className="mark" aria-hidden="true">{outcome === "done" ? "✓" : "✕"}</span>
          <span>{outcome === "done" ? (job.summary || "Done.") : (job.error || outcome)}</span>
        </div>
      )}
      <div className="job-foot">
        {live ? `cap $${job.budget_usd ?? "?"} · ${Math.round((job.timeout_s || 3600) / 60)} min` :
          `${job.cost_usd != null ? `$${job.cost_usd} · ` : ""}${outcome === "done" ? "saved to notes" : outcome}`}
      </div>
    </aside>
  );
}
