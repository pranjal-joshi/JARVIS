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
    case "ask_claude": return { glyph: "⟁", title: "EXPERT UPLINK · CLAUDE", detail: quoted(a.question) };
    case "ask_opencode": return { glyph: "⟁", title: "EXPERT UPLINK · OPENCODE", detail: quoted(a.question) };
    case "get_time": return { glyph: "◷", title: "CHRONO", detail: clip(a.timezone || "local") };
    case "calculate": return { glyph: "∑", title: "COMPUTE", detail: clip(a.expression) };
    case "weather": return { glyph: "☁", title: "ATMOSPHERICS", detail: clip(a.place) };
    case "fetch_page": return { glyph: "⌖", title: "PAGE READ", detail: clip(a.url) };
    case "set_timer": return { glyph: "◴", title: "TIMER SET", detail: clip(a.label || `${a.minutes || 0}m ${a.seconds || 0}s`) };
    case "list_timers": return { glyph: "◴", title: "TIMERS", detail: "running" };
    case "cancel_timer": return { glyph: "◴", title: "TIMER CANCEL", detail: clip(a.label) };
    case "kb_forget": return { glyph: "✖", title: "MEMORY ERASE", detail: clip(a.page) };
    case "adb_notifications": return { glyph: "◉", title: "NOTIFICATIONS", detail: clip(a.app || "all apps") };
    case "adb_sms_read": return { glyph: "✉", title: "SMS READ", detail: clip(a.sender || "inbox") };
    case "adb_call_log": return { glyph: "☏", title: "CALL LOG", detail: "recent calls" };
    case "adb_contacts": return { glyph: "☏", title: "CONTACT LOOKUP", detail: clip(a.name) };
    case "adb_sms_send": return { glyph: "✉", title: "SMS SEND", detail: clip(`${a.to}: ${a.text}`) };
    case "adb_whatsapp_send": return { glyph: "✉", title: "WHATSAPP SEND", detail: clip(`${a.to}: ${a.text}`) };
    case "adb_call": return { glyph: "☏", title: "CALL", detail: clip(a.to) };
    case "adb_call_answer": return { glyph: "☏", title: "ANSWER CALL", detail: "" };
    case "adb_call_end": return { glyph: "☏", title: "HANG UP", detail: "" };
    case "fs_list": return { glyph: "▤", title: "FILE INDEX", detail: clip(a.path || "shared folders") };
    case "fs_read": return { glyph: "▤", title: "FILE READ", detail: clip(a.path) };
    case "win_ps_read": return { glyph: "⌘", title: "SYSTEM QUERY", detail: clip(a.check ? `${a.check} ${a.name || ""}` : a.command) };
    case "win_ps_run": return { glyph: "⌘", title: "SYSTEM COMMAND", detail: clip(a.command) };
    case "kb_clear_memory": return { glyph: "✖", title: "MEMORY WIPE", detail: "all jarvis/ pages" };
    case "kb_delete_page": return { glyph: "✖", title: "PAGE DELETE", detail: clip(a.page) };
    case "kb_delete_item": return { glyph: "✖", title: "ITEM DELETE", detail: clip(`${a.page}: ${a.match}`) };
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
  if (status === "denied" || status === "blocked") return "BLOCKED";
  if (status === "dry-run") return "DRY RUN"; // recorded, not done (evals, new skills)
  if (status === "timeout") return "EXPIRED";
  if (status === "error") return "FAULT";
  if (status === "rejected") return "RETRY"; // refused before running (bad arguments); the model was told why
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
  if (tool?.startsWith("kb_") && /forget|clear|delete/.test(tool)) return "removed";
  if (tool === "web_search") {
    const n = (String(preview).match(/^- /gm) || []).length;
    return n ? `${n} sources` : "no signal";
  }
  if (tool === "ask_claude" || tool === "ask_opencode") {
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
