// Turns raw tool events into short, human-readable timeline rows:
// a sci-fi codename, one line of detail, and an insight from the result.

const clip = (s, n = 38) => {
  const t = String(s ?? "").replace(/\s+/g, " ").trim();
  return t.length > n ? `${t.slice(0, n - 1)}…` : t;
};
const quoted = (s) => (s ? `“${clip(s, 34)}”` : "");

// Home Assistant intent names -> readable action ("HassTurnOn" -> "POWER ON")
const HA = {
  HassTurnOn: ["POWER ON", "⏻"],
  HassTurnOff: ["POWER OFF", "⏻"],
  HassToggle: ["TOGGLE", "⏻"],
  HassLightSet: ["LIGHT TUNE", "✦"],
  HassClimateSetTemperature: ["CLIMATE SET", "❄"],
  HassMediaPause: ["MEDIA HOLD", "▶"],
  HassMediaUnpause: ["MEDIA RESUME", "▶"],
  HassSetVolume: ["VOLUME SET", "▶"],
  GetLiveContext: ["SENSOR SWEEP", "◎"],
  GetDateTime: ["CLOCK SYNC", "◷"],
};

export function describe(tool = "", args = {}) {
  const a = args || {};
  switch (tool) {
    case "kb_search": return { glyph: "◈", title: "MEMORY SCAN", detail: quoted(a.query) };
    case "kb_read_page": return { glyph: "▤", title: "ARCHIVE OPEN", detail: clip(a.page) };
    case "kb_tasks": return { glyph: "☰", title: "TASK SWEEP", detail: "open tasks & deadlines" };
    case "kb_remember": return { glyph: "✎", title: "MEMORY WRITE", detail: clip(a.fact) };
    case "kb_journal": return { glyph: "✎", title: "LOG ENTRY", detail: clip(a.text) };
    case "kb_append_to_page": return { glyph: "✎", title: "PAGE EDIT", detail: clip(a.page) };
    case "web_search": return { glyph: "⌖", title: "NET SWEEP", detail: quoted(a.query) };
    case "ask_expert":
      return { glyph: "⟁", title: `EXPERT UPLINK · ${a.engine === "opencode" ? "OPENCODE" : "CLAUDE"}`, detail: quoted(a.question) };
  }
  const base = tool.includes("__") ? tool.split("__").pop() : tool;
  if (HA[base]) {
    const target = a.name || a.area || a.domain || "";
    return { glyph: HA[base][1], title: HA[base][0], detail: clip(Array.isArray(target) ? target.join(", ") : target) };
  }
  return { glyph: "⌁", title: base.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/_/g, " ").toUpperCase(), detail: "" };
}

// One short insight from a tool result preview.
export function insight(tool, status, preview = "") {
  if (status === "denied") return "BLOCKED";
  if (status === "timeout") return "EXPIRED";
  if (status === "error") return "FAULT";
  if (status === "pending") return "AWAITING CLEARANCE";
  if (status === "running") return "…";
  const lines = String(preview).split("\n").filter((l) => l.trim());
  if (tool === "kb_search") {
    const hits = lines.filter((l) => /^\[[^\]]+\]/.test(l)).length;
    return hits ? `${hits} hit${hits > 1 ? "s" : ""}` : "no match";
  }
  if (tool === "kb_tasks") {
    const n = lines.filter((l) => /^(TODO|LATER|NOW|DOING|WAITING)\b/.test(l.trim())).length;
    return n ? `${n} open` : "clear";
  }
  if (tool?.startsWith("kb_") && /remember|journal|append/.test(tool)) return "saved";
  if (tool === "web_search") {
    const n = (String(preview).match(/^- /gm) || []).length;
    return n ? `${n} sources` : "no signal";
  }
  if (tool === "ask_expert") {
    const s = String(preview).match(/\(([\d.]+) s\)/);
    return s ? `answered · ${Math.round(+s[1])}s` : "answered";
  }
  return "done";
}

export const SOURCE = { web: "HUD", satellite: "VOICE", phone: "PHONE" };

export function ago(ts, now = Date.now() / 1000) {
  const s = Math.max(0, Math.round(now - ts));
  if (s < 5) return "now";
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}
