"use client";

// One background job, beside the orb: its goal, how long it has run, and its steps (the job's own to-do list
// when it keeps one, else the last steps it took). A running card can be stopped; once the job ends the card
// shows the outcome and stays until it is dismissed. Several run at once as a stack (Hud.jsx).
// The card grows with the job: a queued job is a slim pill, a running one gets a progress ring (its to-do list
// done / total, or a sweeping arc when it keeps none), a finished one a result card with where it was saved.

import { useEffect, useState } from "react";

const left = (s) => {
  s = Math.max(0, Math.round(s));
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  if (s >= 60) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  return `${s}s`;
};
const MARK = { completed: "✓", in_progress: "◉", pending: "○" };
const R = 17, C = 2 * Math.PI * R;

function Ring({ pct, tone }) {
  return (
    <svg className={`job-ring${pct == null ? " sweep" : ""}`} viewBox="0 0 44 44" aria-hidden="true">
      <circle className="bg" cx="22" cy="22" r={R} />
      <circle className="fg" cx="22" cy="22" r={R} strokeDasharray={C}
        strokeDashoffset={pct == null ? C * 0.72 : C * (1 - pct / 100)} style={tone ? { stroke: tone } : undefined} />
      <text x="22" y="26" textAnchor="middle">{pct == null ? "…" : `${pct}`}</text>
    </svg>
  );
}

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
  const pct = todos.length ? Math.round(100 * todos.filter((t) => t.status === "completed").length / todos.length) : null;

  if (job.state === "queued") { // a slim pill until it starts
    return (
      <aside className={`job-box job-pill${closing ? " closing" : ""}`} aria-label={`Queued job: ${job.goal}`}>
        <i className="job-pulse" aria-hidden="true" /><span className="job-kind">QUEUED</span>
        <span className="job-goal" title={job.goal}>{job.goal}</span>
        <button type="button" className="job-x" title="Stop this job" aria-label="Stop this job"
          disabled={stopping} onClick={() => { setStopping(true); onCancel(job.id); }}>■</button>
      </aside>
    );
  }

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
      <div className="job-body">
      <Ring pct={live ? pct : 100} tone={live ? null : outcome === "done" ? "#7dffb0" : "var(--red)"} />
      <div className="job-main">
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
        <>
          <div className="job-outcome">
            <span className="mark" aria-hidden="true">{outcome === "done" ? "✓" : "✕"}</span>
            <span>{outcome === "done" ? (job.summary || "Done.") : (job.error || outcome)}</span>
          </div>
          {outcome === "done" && (
            <div className="job-chips">
              <span className="job-chip">saved → {job.page || "jarvis/jobs"}</span>
              {job.steps ? <span className="job-chip">{job.steps} steps</span> : null}
              {job.cost_usd != null && <span className="job-chip">${job.cost_usd}</span>}
            </div>
          )}
        </>
      )}
      </div>
      </div>
      <div className="job-foot">
        {live ? `cap $${job.budget_usd ?? "?"} · ${Math.round((job.timeout_s || 3600) / 60)} min` :
          `${job.cost_usd != null ? `$${job.cost_usd} · ` : ""}${outcome === "done" ? "saved to notes" : outcome}`}
      </div>
    </aside>
  );
}
