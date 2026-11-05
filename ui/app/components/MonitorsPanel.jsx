"use client";

// Monitors: what JARVIS is keeping an eye on right now. Watchers (look-only checks you set up in the
// watch panel), running timers and scheduled skills, each with a countdown. The panel lights up and grows
// (like the knowledge map) when one fires, starts, fails or is cancelled; the row that changed animates:
// fired = a ripple and a sweep, set/created = a scan-in, cancelled/deleted = strike and dissolve, failed = shake.

import { useEffect, useState } from "react";
import { ago } from "../lib/timeline";

const left = (s) => {
  s = Math.max(0, Math.round(s));
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  if (s >= 60) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  return `${s}s`;
};
const PERIOD = { m: 60, h: 3600 };
const period = (every) => { const m = /^(\d+)([mh])$/.exec(every || ""); return m ? +m[1] * PERIOD[m[2]] : 300; };
const TARGET = { windows: "⊞", android: "▯", hud: "◫", voice: "◖" };

// a thin ring that empties as the next run (or the timer's end) comes closer
function Ring({ frac, warn }) {
  const r = 9, c = 2 * Math.PI * r;
  return (
    <svg className={`mon-ring${warn ? " warn" : ""}`} viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r={r} className="track" />
      <circle cx="12" cy="12" r={r} className="arc" strokeDasharray={c}
        strokeDashoffset={c * (1 - Math.max(0, Math.min(1, frac)))} />
    </svg>
  );
}

export default function MonitorsPanel({ data, pulse, ghosts, active, focused, headProps, onManage, onClearJobs }) {
  const [now, setNow] = useState(() => Date.now() / 1000); // its own clock, so only this panel ticks
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(id);
  }, []);
  const watchers = data?.watchers || [];
  const timers = data?.timers || [];
  const schedules = (data?.schedules || []).filter((s) => s.enabled);
  const jobs = data?.jobs || []; // background jobs: running ones, and those that ended in the last hours
  const fx = (kind, name) => (pulse && pulse.name === name && pulse.kind === kind ? ` fx-${kind}` : "");
  const rowFx = (name) => (pulse && pulse.name === name ? ` fx-${pulse.kind}` : "");
  const count = watchers.filter((w) => w.enabled).length + timers.length + schedules.length
    + jobs.filter((j) => j.state === "running" || j.state === "queued").length;

  return (
    <section className={`panel monitors${active ? ` active m-${pulse?.kind || "ran"}` : ""}${focused ? " focused" : ""}`}
      aria-label="Monitors">
      {active && <div key={pulse?.seq} className="mon-sweep" aria-hidden="true" />}
      <div className="kb-head">
        <h2 {...headProps}>
          Monitors
          <span className="kb-state">
            {active ? { fired: "alert", failed: "fault", cancelled: "cancelled", deleted: "removed", set: "armed",
              created: "armed", done: "done", learned: "learning", paused: "paused", resumed: "resumed" }[pulse?.kind] || "live"
              : `${count} live`}
            <b className="expand" aria-hidden="true" />
          </span>
        </h2>
        <div className="win-btns">
          {onClearJobs && jobs.some((j) => !(j.state === "running" || j.state === "queued")) && (
            <button type="button" className="mon-clear" onClick={(e) => { e.stopPropagation(); onClearJobs(); }}
              title="Clear finished background jobs from this list (their reports stay in your notes)"
              aria-label="Clear finished jobs">clear</button>
          )}
          <button type="button" onClick={onManage} title="Add or change watchers" aria-label="Manage watchers">＋</button>
        </div>
      </div>
      {data?.error && <p className="mon-error">bridge: {data.error}</p>}
      <ul className="mon-list">
        {watchers.map((w) => {
          const p = period(w.every);
          const next = w.last_run ? w.last_run + p - now : 0;
          const state = !w.enabled ? "paused" : w.last_error ? "error" : w.running ? "running" : "ok";
          return (
            <li key={`w-${w.name}`} className={`mon w ${state}${rowFx(w.name)}`}
              title={[w.description, w.last_error && `last error: ${w.last_error}`].filter(Boolean).join("\n")}>
              <span className="mon-glyph" aria-hidden="true">
                {w.enabled && !w.running ? <Ring frac={next / p} warn={!!w.last_error} /> : <i className="mon-dot" />}
                {fx("fired", w.name) && <i key={pulse.seq} className="mon-ping" />}
              </span>
              <div className="mon-main">
                <div className="mon-title">{w.title || w.name}
                  <span className="mon-targets">{(w.notify || []).map((t) => TARGET[t]).join(" ")}</span></div>
                <div className="mon-detail">
                  {w.recent?.[0] ? `↳ ${w.recent[0].text}` : w.description || w.name}
                </div>
              </div>
              <div className="mon-meta">
                <span>{!w.enabled ? "paused" : w.running ? "checking…" : w.last_error ? "error"
                  : w.last_run ? `in ${left(next)}` : "first look…"}</span>
                <time>{w.last_fired ? `fired ${ago(w.last_fired, now)}` : `every ${w.every}`}</time>
              </div>
            </li>
          );
        })}
        {timers.map((t) => {
          const remain = t.due - now;
          return (
            <li key={`t-${t.id}`} className={`mon t${rowFx(t.label)}`}>
              <span className="mon-glyph" aria-hidden="true"><Ring frac={Math.min(1, remain / 3600)} warn={remain < 60} /></span>
              <div className="mon-main">
                <div className="mon-title">◴ {t.label}</div>
                <div className="mon-detail">timer</div>
              </div>
              <div className="mon-meta"><span className={remain < 60 ? "soon" : ""}>{left(remain)}</span>
                <time>{new Date(t.due * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time></div>
            </li>
          );
        })}
        {schedules.map((s) => (
          <li key={`s-${s.name}`} className={`mon s${rowFx(s.name)}`}>
            <span className="mon-glyph" aria-hidden="true"><i className="mon-dot" /></span>
            <div className="mon-main">
              <div className="mon-title">⚙ {s.name.replace(/^skills\//i, "")}</div>
              <div className="mon-detail">skill · {s.schedule}{s.dry_run ? " · dry run" : ""}</div>
            </div>
            <div className="mon-meta"><span>{s.schedule}</span>
              <time>{s.last_run ? `ran ${ago(s.last_run, now)}` : "not yet"}</time></div>
          </li>
        ))}
        {jobs.map((j) => {
          const live = j.state === "running" || j.state === "queued";
          const state = live ? "running" : j.state === "done" ? "ok" : j.state === "failed" ? "error" : "paused";
          return (
            <li key={`j-${j.id}`} className={`mon j ${state}${rowFx(j.id)}`}
              title={[j.goal, j.target && `target: ${j.target}`, j.error && `error: ${j.error}`, j.dir].filter(Boolean).join("\n")}>
              <span className="mon-glyph" aria-hidden="true">
                {live ? <Ring frac={Math.max(0, 1 - (now - (j.started || j.created)) / (j.timeout_s || 3600))} />
                  : <i className="mon-dot" />}
              </span>
              <div className="mon-main">
                <div className="mon-title">⛭ {j.skill || "background job"}
                  <span className="mon-targets">{j.id}</span></div>
                <div className="mon-detail">{live ? `↳ ${j.last || "starting…"}` : j.error || j.summary || j.goal}</div>
              </div>
              <div className="mon-meta">
                <span>{live ? `${left(now - (j.started || j.created))} · ${j.steps || 0} steps` : j.state}</span>
                <time>{live ? `cap $${j.budget_usd}` : `${j.cost_usd != null ? `$${j.cost_usd} · ` : ""}${ago(j.ended, now)}`}</time>
              </div>
            </li>
          );
        })}
        {(ghosts || []).map((g) => (
          <li key={`g-${g.seq}`} className={`mon ghost fx-${g.kind}`} aria-hidden="true">
            <span className="mon-glyph"><i className="mon-dot" /></span>
            <div className="mon-main"><div className="mon-title">{g.name}</div>
              <div className="mon-detail">{g.kind === "done" ? "done" : g.kind}</div></div>
          </li>
        ))}
        {!count && !ghosts?.length && (
          <li className="empty">Nothing watched yet. <button type="button" className="linkish" onClick={onManage}>Add a watcher</button></li>
        )}
      </ul>
    </section>
  );
}
