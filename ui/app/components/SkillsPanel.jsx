"use client";

// Skills: knowledge-base pages under skills/ that run when asked or when an event fires (a phone joins, a timer
// ends, a schedule). A skill JARVIS writes is on at once; a page changed outside JARVIS (Obsidian, Sync) waits
// here as "changed" until you approve it, which pins that version.

import { useCallback, useEffect, useState } from "react";

const api = (path, init) => fetch(`/api/hub/${path}`, { cache: "no-store", ...init });
const when = (s) => (s.trigger === "schedule" ? `schedule ${s.schedule || "?"}` : s.trigger || "no trigger")
  + (s.match ? ` · “${s.match}”` : "");

export default function SkillsPanel({ onCount, version }) {
  const [data, setData] = useState(null);

  const load = useCallback(async () => {
    const r = await api("skills").catch(() => null);
    if (!r?.ok) return;
    const d = await r.json();
    setData(d);
    onCount?.(d.skills.filter((s) => s.enabled && !s.changed).length);
  }, [onCount]);

  useEffect(() => { load(); }, [load, version]);

  const post = async (name, what, body) => {
    await api(`skills/${encodeURIComponent(name)}/${what}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
    });
    load();
  };

  return (
    <div className="dev-panel">
      <p className="tools-note">
        Skills are knowledge-base pages under <b>skills/</b> with a <b>trigger</b> property. One JARVIS writes is on at
        once; one changed outside JARVIS waits as <b>changed</b> until you approve it here. Ask JARVIS to make, change,
        run, switch off or delete one (“make a skill that…”).
      </p>
      <ul className="skill-list">
        {(data?.skills || []).map((s) => (
          <li key={s.name}>
            <div className="skill-head">
              <b>{s.name.replace(/^skills\//i, "")}</b>
              {s.enabled && s.changed ? (
                <button type="button" className="gate ask" onClick={() => post(s.name, "enable", { enabled: true })}
                  title="Changed outside JARVIS: read the steps below, then approve this version">
                  changed · approve
                </button>
              ) : (
                <button type="button" className={`gate ${s.enabled ? "auto" : "ask"}`}
                  onClick={() => post(s.name, "enable", { enabled: !s.enabled })}
                  title={s.enabled ? "Switch off" : "Switch back on (approves this version)"}>
                  {s.enabled ? "on" : "off"}
                </button>
              )}
              <button type="button" className="gate" disabled={!s.enabled || s.changed} onClick={() => post(s.name, "run")}
                title="Run it now">run</button>
            </div>
            <i>{when(s)}{s.last_run ? ` · ran ${new Date(s.last_run * 1000).toLocaleTimeString()}` : ""}</i>
            <small>{s.instructions}</small>
          </li>
        ))}
        {data && !data.skills.length && <li className="kbx-dim">No skills yet. Ask JARVIS to make one, or add a page under skills/.</li>}
      </ul>
      {data?.events?.length > 0 && (
        <div className="hook-log">
          <h4>Recent events</h4>
          {data.events.slice(0, 6).map((e, i) => (
            <div key={i}><span>{new Date(e.ts * 1000).toLocaleTimeString()}</span> {e.kind} · {e.text}</div>
          ))}
        </div>
      )}
    </div>
  );
}
