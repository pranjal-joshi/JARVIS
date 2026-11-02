"use client";

// Skills: knowledge-base pages under skills/ that run when an event fires (a phone joins, a timer
// ends, a schedule). A page only runs after it is enabled here, and only while it is unchanged.

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
    onCount?.(d.skills.filter((s) => s.enabled).length);
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
        Skills are knowledge-base pages under <b>skills/</b> with a <b>trigger::</b> property. They run only once you
        enable them here; editing a page switches it off until you enable it again. A newly enabled skill runs as a
        <b>dry run</b> (it shows in the console what it would do) until you let it go live.
      </p>
      <ul className="skill-list">
        {(data?.skills || []).map((s) => (
          <li key={s.name}>
            <div className="skill-head">
              <b>{s.name.replace(/^skills\//i, "")}</b>
              <button type="button" className={`gate ${s.enabled ? "auto" : "ask"}`}
                onClick={() => post(s.name, "enable", { enabled: !s.enabled })}
                title={s.enabled ? "Switch off" : "Allow this skill to run on its trigger"}>
                {s.enabled ? "on" : s.changed ? "edited · enable" : "enable"}
              </button>
              <button type="button" className="gate" disabled={!s.enabled} onClick={() => post(s.name, "run")}
                title="Run it now">run</button>
              {s.enabled && (
                <button type="button" className={`gate ${s.dry_run ? "ask" : "auto"}`}
                  onClick={() => post(s.name, "promote", { enabled: s.dry_run })}
                  title={s.dry_run ? "Dry run: it only shows what it would do. Let it act for real."
                    : "It acts for real. Back to dry run."}>
                  {s.dry_run ? "dry run · go live" : "live"}
                </button>
              )}
            </div>
            <i>{when(s)}{s.last_run ? ` · ran ${new Date(s.last_run * 1000).toLocaleTimeString()}` : ""}</i>
            <small>{s.instructions}</small>
          </li>
        ))}
        {data && !data.skills.length && <li className="kbx-dim">No skill pages yet. Add one in Logseq under skills/.</li>}
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
