"use client";

// Start-up screen: the "waiting" Thinking Orb while the HUD links to the hub and loads its panels.
// Each line ticks off as its data arrives; the screen fades away once everything is in (or after a
// timeout, so a slow or missing service never traps the user behind it).

import { Orb as DotOrb } from "@yogesharc/thinking-orbs";

export default function BootScreen({ steps, leaving }) {
  const done = steps.filter((s) => s.state === "ok").length;
  return (
    <div className={`boot${leaving ? " leaving" : ""}`} role="status" aria-live="polite"
      aria-label={`Initialising, ${done} of ${steps.length} systems ready`}>
      <div className="boot-orb" aria-hidden="true">
        <span className="boot-ring" />
        <DotOrb state="waiting" variant="default" size={150} density={1.15} dotSize={1.15} />
      </div>
      <h1 className="boot-title">J.A.R.V.I.S.</h1>
      <p className="boot-sub">initialising · {done}/{steps.length}</p>
      <ul className="boot-steps">
        {steps.map((s, i) => (
          <li key={s.label} className={s.state} style={{ "--i": i }}>
            <span className="mark" aria-hidden="true">{s.state === "ok" ? "✓" : s.state === "fail" ? "✕" : "·"}</span>
            {s.label}
            <i>{s.state === "ok" ? "online" : s.state === "fail" ? s.note || "unavailable" : "linking"}</i>
          </li>
        ))}
      </ul>
      <div className="boot-bar" aria-hidden="true"><span style={{ transform: `scaleX(${done / steps.length})` }} /></div>
    </div>
  );
}
