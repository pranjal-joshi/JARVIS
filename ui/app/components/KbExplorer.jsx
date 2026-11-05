"use client";

// Full-screen knowledge-graph explorer: fly around the graph, click a page to read it (the graph keeps its
// neighbourhood bright), follow its links and the pages linking to it, add pages to the chat as context (button,
// or drag the page onto the chat box). Points can be edited, moved to another page, added or deleted (the bin,
// then confirm); pages renamed (links follow) or deleted. Every change keeps the page as it was in .trash/, and
// the trash view puts any of those copies back. With "tools & skills" on, what JARVIS can do is on the map too:
// a tool opens its card; a skill is a page, read and edited like any other. Keys: / finds, ↑↓ Enter pick from the
// list, Esc goes back. "dream" runs tonight's memory consolidation now (kb/dreaming.py) and opens what it kept.

import { useEffect, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";

const KbGraph = dynamic(() => import("./KbGraph"), {
  ssr: false,
  loading: () => <div className="kb-canvas kb-loading">initialising graph…</div>,
});

export const PAGE_MIME = "application/x-jarvis-page";
const api = (path) => fetch(`/api/hub/${path}`, { cache: "no-store" });
const post = (path, body) => fetch(`/api/hub/${path}`, {
  method: "POST", headers: { "Content-Type": "application/json" }, cache: "no-store", body: JSON.stringify(body),
}).catch(() => null);
const why = async (r, fallback) => (await r?.json().catch(() => ({})))?.detail || fallback;

// "3 min ago", "yesterday", "12 Oct": how long since a page changed
function ago(seconds) {
  if (!seconds) return "";
  const s = Date.now() / 1000 - seconds;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 172800) return "yesterday";
  if (s < 30 * 86400) return `${Math.round(s / 86400)} days ago`;
  return new Date(seconds * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

// drawn in the text colour so they follow the button's hover and confirm colours
const Icon = ({ d }) => (
  <svg className="kbx-bin" viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" fill="none" stroke="currentColor"
    strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d={d} /></svg>
);
const Bin = () => <Icon d="M2.5 4h11M6.5 4V2.5h3V4M4 4l.7 9.5h6.6L12 4M6.8 6.5v4.5M9.2 6.5v4.5" />;
const Pen = () => <Icon d="M10.5 2.5l3 3L6 13H3v-3zM9 4l3 3" />;
const Move = () => <Icon d="M2.5 8h10M9.5 4.5L13 8l-3.5 3.5" />;

// a point's text: [[links]] open that page, web links open in a new tab (a remembered fact's source)
function Said({ text, onOpen }) {
  return text.split(/(\[\[[^\]]+\]\]|https?:\/\/[^\s)]+)/g).map((p, i) => {
    const m = p.match(/^\[\[([^\]]+)\]\]$/);
    if (m) return <button key={i} type="button" className="kbx-link" onClick={() => onOpen(m[1])}>{m[1]}</button>;
    if (/^https?:\/\//.test(p)) {
      let host = p;
      try { host = new URL(p).hostname.replace(/^www\./, ""); } catch { /* keep the text */ }
      return <a key={i} className="kbx-link kbx-src" href={p} target="_blank" rel="noreferrer" title={p}>{host}</a>;
    }
    return <span key={i}>{p}</span>;
  });
}

// one point: its text and where it came from; edit, move or delete it (delete asks once more)
function Block({ said, props, onOpen, onDelete, onEdit, onMove, pages }) {
  const [mode, setMode] = useState(null); // null | "edit" | "move" | "delete"
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const act = (fn) => async () => {
    setBusy(true);
    const ok = await fn();
    setBusy(false);
    if (ok !== false) setMode(null);
  };
  const keys = (save) => (e) => {
    if (e.key === "Escape") { e.stopPropagation(); setMode(null); }
    if (e.key === "Enter" && (mode === "move" || !e.shiftKey)) { e.preventDefault(); save(); }
  };
  const origin = [props?.learned, props?.via].filter(Boolean).join(" · ");
  const save = act(() => (draft.trim() && draft.trim() !== said ? onEdit(draft) : true));
  const move = act(() => (draft.trim() ? onMove(draft.trim()) : false));

  return (
    <li className={mode === "delete" ? "kbx-asking" : ""}>
      <span className="kbx-text">
        {mode === "edit" ? (
          <textarea className="kbx-edit" autoFocus value={draft} rows={Math.min(6, draft.split("\n").length + 1)}
            onChange={(e) => setDraft(e.target.value)} onKeyDown={keys(save)} aria-label="Edit this point" />
        ) : <Said text={said} onOpen={onOpen} />}
        {origin && mode !== "edit" && <small className="kbx-origin">{origin}</small>}
        {mode === "move" && (
          <span className="kbx-moveto">
            <input autoFocus list="kbx-pages" placeholder="to page…" value={draft} disabled={busy}
              onChange={(e) => setDraft(e.target.value)} onKeyDown={keys(move)} aria-label="Move to page" />
            <button type="button" className="kbx-del yes" disabled={busy || !draft.trim()} onClick={move}>{busy ? "…" : "move"}</button>
          </span>
        )}
      </span>
      {onDelete && (mode === "delete" || mode === "edit" ? (
        <span className="kbx-confirm">
          {mode === "delete" ? (
            <button type="button" className="kbx-del yes" disabled={busy} onClick={act(onDelete)}
              aria-label="Confirm: delete this point">{busy ? "…" : "delete"}</button>
          ) : (
            <button type="button" className="kbx-del yes" disabled={busy} onClick={save}>{busy ? "…" : "save"}</button>
          )}
          <button type="button" className="kbx-del" disabled={busy} onClick={() => setMode(null)}>
            {mode === "delete" ? "keep" : "cancel"}</button>
        </span>
      ) : (
        <span className="kbx-tools">
          <button type="button" className="kbx-del" onClick={() => { setDraft(said); setMode("edit"); }}
            title="Edit this point" aria-label="Edit this point"><Pen /></button>
          {pages && <button type="button" className="kbx-del" onClick={() => { setDraft(""); setMode(mode === "move" ? null : "move"); }}
            title="Move this point to another page" aria-label="Move this point"><Move /></button>}
          <button type="button" className="kbx-del" onClick={() => setMode("delete")}
            title="Delete this point (and anything nested under it)" aria-label="Delete this point"><Bin /></button>
        </span>
      ))}
    </li>
  );
}

// deletes the open page after a second click, like a point
function PageDelete({ onDelete }) {
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const remove = async () => {
    setBusy(true);
    await onDelete();
    setBusy(false);
    setAsking(false);
  };
  return asking ? (
    <span className="kbx-confirm kbx-page-del">
      <button type="button" className="kbx-del yes" disabled={busy} onClick={remove}
        aria-label="Confirm: delete this whole page">{busy ? "…" : "delete page"}</button>
      <button type="button" className="kbx-del" disabled={busy} onClick={() => setAsking(false)}
        aria-label="Keep this page">keep</button>
    </span>
  ) : (
    <button type="button" className="kbx-btn kbx-page-del" onClick={() => setAsking(true)}
      title="Delete this whole page (a copy is kept in .trash)"><Bin /> delete page</button>
  );
}

// a tool's card, or a family's tools: what it does, how risky it is, what it takes
function ToolCard({ node, nodes, onPick }) {
  if (node.kind === "family") {
    const tools = nodes.filter((n) => n.kind === "tool" && (node.id === "tools" || `tools/${n.family}` === node.id));
    const fams = node.id === "tools" ? [...new Set(tools.map((t) => t.family))].sort() : null;
    return (
      <div className="kbx-tool">
        <p className="kbx-dim">{fams ? `${tools.length} tools in ${fams.length} families` : `${tools.length} tools`}</p>
        <div className="kbx-refs">
          {(fams || tools.map((t) => t.label)).map((x) => (
            <button key={x} type="button" className="kbx-link"
              onClick={() => onPick(fams ? nodes.find((n) => n.id === `tools/${x}`) : tools.find((t) => t.label === x))}>{x}</button>
          ))}
        </div>
      </div>
    );
  }
  return (
    <div className="kbx-tool">
      <p className="kbx-tool-tags">
        <span className={`kbx-risk ${node.risk}`}>{node.risk}</span>
        {node.asks && <span className="kbx-risk ask">asks you first</span>}
        <button type="button" className="kbx-link" onClick={() => onPick(nodes.find((n) => n.id === `tools/${node.family}`))}>
          {node.family}</button>
        {node.origin !== "builtin" && <span className="kbx-dim">from {node.origin}</span>}
      </p>
      <p className="kbx-tool-desc">{node.description}</p>
      {node.params?.length > 0 && <p className="kbx-dim">takes {node.params.join(", ")}</p>}
    </div>
  );
}

const skillState = (n) => (n.enabled ? "on" : "off");
const skillWhen = (n) => (n.trigger === "schedule" ? `runs ${/^every/.test(n.schedule) ? n.schedule : `daily at ${n.schedule}`}`
  : n.trigger === "manual" ? "runs when asked" : `runs on ${n.trigger}`);

// what .trash/ holds: a deleted page, or a page as it was before a point went; restore puts it back
function Trash({ onRestored }) {
  const [items, setItems] = useState(null);
  const [asking, setAsking] = useState(null);
  const [failed, setFailed] = useState("");
  const load = () => api("kb/trash").then((r) => (r.ok ? r.json() : [])).then(setItems).catch(() => setItems([]));
  useEffect(() => { load(); }, []);
  const restore = async (t) => {
    const r = await post("kb/trash/restore", { id: t.id });
    if (!r?.ok) { setFailed(await why(r, "Couldn't restore that.")); return; }
    setAsking(null);
    onRestored((await r.json()).page);
  };
  if (!items) return <p className="kbx-dim">reading the trash…</p>;
  return (
    <div className="kbx-trash">
      <p className="kbx-dim">Every deleted page, and every page as it was before a point was changed or deleted.</p>
      {failed && <p className="kbx-dim kbx-failed" role="alert">{failed}</p>}
      <ul className="kbx-trash-list">
        {items.map((t) => (
          <li key={t.id} className="kbx-trash-item">
            <span><b className={t.journal ? "journal" : ""}>{t.page}</b>
              <small>{ago(t.at)} · {t.points} point{t.points === 1 ? "" : "s"}{t.exists ? " · earlier version" : " · deleted"}</small></span>
            {asking === t.id ? (
              <span className="kbx-confirm">
                <button type="button" className="kbx-del yes" onClick={() => restore(t)}>replace current</button>
                <button type="button" className="kbx-del" onClick={() => setAsking(null)}>keep</button>
              </span>
            ) : (
              <button type="button" className="kbx-del" title={t.exists ? "Put this version back (the current page goes to the trash)" : "Put this page back"}
                onClick={() => (t.exists ? setAsking(t.id) : restore(t))}>restore</button>
            )}
          </li>
        ))}
        {!items.length && <li className="kbx-dim">The trash is empty.</li>}
      </ul>
    </div>
  );
}

export default function KbExplorer({ version, context, onAdd, onClose }) {
  const [nodes, setNodes] = useState([]);
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0); // the list's keyboard pick
  const [selected, setSelected] = useState(null); // node id
  const [page, setPage] = useState(null); // /kb/page's answer | { name, loading } | { name, error }
  const [trash, setTrash] = useState(false);
  const [renaming, setRenaming] = useState(null); // the new name being typed
  const [adding, setAdding] = useState("");
  const [changed, setChanged] = useState(0); // something was written: the page list's counts are stale
  const [failed, setFailed] = useState("");
  const [dream, setDream] = useState(null); // null | "…" while dreaming | what the dream kept, in a sentence
  const [agent, setAgent] = useState(() => { try { return localStorage.getItem("kbx-agent") !== "0"; } catch { return true; } });
  const searchRef = useRef(null);
  const listRef = useRef(null);
  const view = useRef({});

  useEffect(() => {
    api(`kb/graph${agent ? "?agent=true" : ""}`).then((r) => (r.ok ? r.json() : { nodes: [] }))
      .then((g) => setNodes(g.nodes || [])).catch(() => {});
  }, [version, changed, agent]);
  const showAgent = (on) => {
    setAgent(on);
    try { localStorage.setItem("kbx-agent", on ? "1" : "0"); } catch { /* per-viewer nicety only */ }
  };

  const back = () => { setPage(null); setSelected(null); setRenaming(null); setFailed(""); };
  view.current = { page, trash, back, onClose, setTrash };

  // Esc: out of trash, then back from a page, then close; Backspace outside a field goes back; / finds
  useEffect(() => {
    searchRef.current?.focus();
    const onKey = (e) => {
      const typing = /^(INPUT|TEXTAREA)$/.test(e.target.tagName);
      const v = view.current;
      if (e.key === "Escape") {
        if (v.trash) v.setTrash(false);
        else if (v.page) v.back();
        else v.onClose();
      } else if (e.key === "Backspace" && !typing && v.page) {
        e.preventDefault();
        v.back();
      } else if (e.key === "/" && !typing) {
        e.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // a node from the graph or the list: a tool or a family opens its card, anything else is a page
  // dream now (it also runs every night): go through the last days' notes, keep what matters on jarvis/dreams
  const dreamNow = async () => {
    setDream("…");
    const r = await post("dream/run", {});
    if (!r?.ok) { setDream(null); setFailed(await why(r, "Couldn't dream just now.")); return; }
    const out = await r.json();
    setDream(out.said);
    setChanged((n) => n + 1);
    open(out.added?.length || out.merged?.length || out.updated?.length ? "jarvis/dreams" : "jarvis/dreams/diary");
  };

  const pick = (n) => {
    if (!n) return back();
    if (n.kind === "tool" || n.kind === "family") {
      setTrash(false);
      setSelected(n.id);
      setRenaming(null);
      setFailed("");
      setPage({ name: n.label, tool: n });
      return undefined;
    }
    return open(n.label);
  };

  const open = async (name, quiet = false) => {
    const id = String(name).toLowerCase();
    setTrash(false);
    setSelected(id);
    setRenaming(null);
    if (!quiet) { setFailed(""); setPage({ name, loading: true }); }
    const r = await api(`kb/page?name=${encodeURIComponent(name)}`).catch(() => null);
    setPage(r?.ok ? await r.json() : { name, error: "Not a page yet: only linked to." });
  };

  // a change by hand: report a refusal, then show the page (and the graph) as they are now
  const write = async (path, body, fallback, reopen = page?.name) => {
    const r = await post(path, body);
    const ok = !!r?.ok;
    setFailed(ok ? "" : await why(r, fallback));
    const out = ok ? await r.json().catch(() => ({})) : {};
    if (reopen) await open(out.page && path === "kb/page/rename" ? out.page : reopen, true);
    setChanged((n) => n + 1);
    return ok;
  };
  const point = (i) => ({ page: page.name, index: page.index[i], text: page.blocks[i] });
  const removeBlock = (i) => () => write("kb/block/delete", point(i), "Couldn't delete that point.");
  const editBlock = (i) => (text) => write("kb/block/edit", { ...point(i), new: text }, "Couldn't change that point.");
  const moveBlock = (i) => (to) => write("kb/block/move", { ...point(i), to }, "Couldn't move that point.");
  const addBlock = async () => {
    if (!adding.trim()) return;
    if (await write("kb/block/add", { page: page.name, text: adding.trim() }, "Couldn't add that point.")) setAdding("");
  };
  const rename = async () => {
    const to = renaming?.trim();
    if (!to || to === page.name) { setRenaming(null); return; }
    if (await write("kb/page/rename", { page: page.name, digest: page.digest, to }, "Couldn't rename that page.")) setRenaming(null);
  };

  // the whole page, as it was read (the hub refuses if it changed since); it goes to .trash/
  const removePage = async () => {
    const r = await post("kb/page/delete", { page: page.name, digest: page.digest });
    if (!r?.ok) {
      setFailed(await why(r, "Couldn't delete that page."));
      await open(page.name, true);
      return;
    }
    back();
    setChanged((n) => n + 1);
  };

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = nodes.filter((n) => (n.blocks > 0 || n.kind === "tool") && (!q || n.label.toLowerCase().includes(q)));
    const rank = (n) => (n.journal ? 0 : n.kind === "tool" ? 3 : n.kind === "skill" ? 2 : 1);
    return list.sort((a, b) => rank(a) - rank(b) || b.blocks - a.blocks || a.label.localeCompare(b.label)).slice(0, 40);
  }, [nodes, query]);
  // the graph marks every page the search finds (not just the 40 listed); none while not searching
  const marked = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? nodes.filter((n) => n.label.toLowerCase().includes(q)).map((n) => n.id) : null;
  }, [nodes, query]);
  const pageNames = useMemo(() => nodes.filter((n) => n.blocks > 0 && !n.journal && !n.kind).map((n) => n.label).sort(), [nodes]);
  const skill = page && !page.tool ? nodes.find((n) => n.kind === "skill" && n.id === String(page.name).toLowerCase()) : null;

  useEffect(() => setCursor(0), [query]);
  useEffect(() => {
    listRef.current?.querySelector(`[data-i="${cursor}"]`)?.scrollIntoView({ block: "nearest" });
  }, [cursor]);
  const searchKeys = (e) => {
    if (page || trash) {
      if (e.key === "Enter" && query.trim()) { back(); setTrash(false); }
      return;
    }
    if (e.key === "ArrowDown") { e.preventDefault(); setCursor((c) => Math.min(c + 1, matches.length - 1)); }
    if (e.key === "ArrowUp") { e.preventDefault(); setCursor((c) => Math.max(c - 1, 0)); }
    if (e.key === "Enter" && matches[cursor]) pick(matches[cursor]);
  };

  const inContext = (name) => context.some((c) => c.name.toLowerCase() === String(name).toLowerCase());
  const add = (p) => p?.blocks && onAdd({ name: p.name, text: (p.shown || p.blocks).map((b) => `- ${b}`).join("\n") });
  const drag = (name) => (e) => {
    e.dataTransfer.setData(PAGE_MIME, name);
    e.dataTransfer.setData("text/plain", `[[${name}]]`);
    e.dataTransfer.effectAllowed = "copy";
  };

  return (
    <div className="kbx" role="dialog" aria-modal="true" aria-label="Knowledge graph explorer">
      <div className="kbx-frame">
        <header className="kbx-head">
          <h2>Knowledge graph <small>{nodes.filter((n) => n.blocks && !n.kind).length} pages
            {agent && ` · ${nodes.filter((n) => n.kind === "tool").length} tools · ${nodes.filter((n) => n.kind === "skill").length} skills`}
            {" "}· click to read · drag to chat · / to find</small></h2>
          <span className="kbx-head-tools">
            <label className="kbx-toggle" title="Show JARVIS's tools and skills on the map">
              <input type="checkbox" checked={agent} onChange={(e) => showAgent(e.target.checked)} /> tools &amp; skills
            </label>
            <button type="button" className="kbx-btn" onClick={dreamNow} disabled={dream === "…"}
              title="Go through the last days' notes and conversations and keep what matters on jarvis/dreams (also every night)">
              {dream === "…" ? "dreaming…" : "☾ dream"}</button>
            <button type="button" className={`kbx-btn${trash ? " primary" : ""}`} onClick={() => { setTrash((t) => !t); setPage(null); setSelected(null); }}
              title="Deleted pages and earlier versions">{trash ? "close trash" : <><Bin /> trash</>}</button>
            <button type="button" className="kbx-close" onClick={onClose} title="Close (Esc)" aria-label="Close explorer">✕</button>
          </span>
        </header>
        <div className="kbx-body">
          <div className="kbx-graph">
            <KbGraph interactive agent={agent} version={`${version}:${changed}`} selected={selected} highlight={marked}
              onPick={pick} />
            <div className="kbx-legend" aria-hidden="true">
              <span className="dot page" />page <span className="dot jarvis" />jarvis memory <span className="dot journal" />journal
              {agent && <><span className="dot tool" />tool <span className="dot skill" />skill</>}
              <span className="kbx-legend-note">brighter = changed lately</span>
            </div>
          </div>
          <aside className="kbx-side">
            <input ref={searchRef} className="kbx-search" placeholder="Find a page…" value={query}
              onChange={(e) => setQuery(e.target.value)} onKeyDown={searchKeys} aria-label="Find a page"
              aria-controls="kbx-matches" />
            <datalist id="kbx-pages">{pageNames.map((n) => <option key={n} value={n} />)}</datalist>
            {dream && dream !== "…" && (
              <p className="kbx-dream" role="status">☾ {dream}
                <button type="button" className="kbx-back" onClick={() => setDream(null)} aria-label="Dismiss">✕</button></p>
            )}
            {trash ? (
              <Trash onRestored={(name) => { setChanged((n) => n + 1); open(name); }} />
            ) : page?.tool ? (
              <article className="kbx-page">
                <div className="kbx-title">
                  <h3 className={`kbx-tool-name ${page.tool.kind}`}>{page.tool.kind === "family" ? `${page.name} tools` : page.name}</h3>
                  <button type="button" className="kbx-back" onClick={back} title="Back (Esc)">back</button>
                </div>
                <ToolCard node={page.tool} nodes={nodes} onPick={pick} />
              </article>
            ) : page ? (
              <article className="kbx-page" draggable={!renaming} onDragStart={drag(page.name)}>
                <div className="kbx-title">
                  <span className="grip" aria-hidden="true">⠿</span>
                  {renaming !== null ? (
                    <input className="kbx-rename" autoFocus value={renaming} onChange={(e) => setRenaming(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") rename();
                        if (e.key === "Escape") { e.stopPropagation(); setRenaming(null); }
                      }}
                      onBlur={() => setRenaming(null)} aria-label="New page name" />
                  ) : (
                    <h3>{page.name}</h3>
                  )}
                  {page.digest && !page.journal && renaming === null && (
                    <button type="button" className="kbx-del" onClick={() => setRenaming(page.name)}
                      title="Rename this page (links to it follow)" aria-label="Rename this page"><Pen /></button>
                  )}
                  <button type="button" className="kbx-back" onClick={back} title="Back (Esc)">back</button>
                </div>
                {page.updated && <p className="kbx-dim kbx-when">changed {ago(page.updated)}</p>}
                {skill && (
                  <p className="kbx-skill">skill · {skillWhen(skill)} · <b className={skill.enabled ? "on" : ""}>{skillState(skill)}</b>
                    {!skill.enabled && <span className="kbx-dim"> · say “turn the {page.name.replace(/^skills\//i, "")} skill back on”</span>}</p>
                )}
                {page.loading && <p className="kbx-dim">reading…</p>}
                {page.error && <p className="kbx-dim">{page.error}</p>}
                {page.blocks && (
                  <>
                    <div className="kbx-actions">
                      <button type="button" className="kbx-btn primary" disabled={inContext(page.name)} onClick={() => add(page)}>
                        {inContext(page.name) ? "✓ in chat context" : "＋ add to chat"}
                      </button>
                      <a className="kbx-btn" href={page.link}>open in Logseq</a>
                      {page.digest && <PageDelete onDelete={removePage} />}
                    </div>
                    {failed && <p className="kbx-dim kbx-failed" role="alert">{failed}</p>}
                    <ul className="kbx-blocks">
                      {page.blocks.map((b, i) => (
                        <Block key={`${page.index?.[i] ?? i}:${b}`} said={page.shown?.[i] ?? b} props={page.props?.[i]}
                          onOpen={open} pages={pageNames.length > 1}
                          onDelete={page.index ? removeBlock(i) : null} onEdit={editBlock(i)} onMove={moveBlock(i)} />
                      ))}
                      {!page.blocks.length && <li className="kbx-dim">Empty page.</li>}
                    </ul>
                    {page.index && (
                      <form className="kbx-add" onSubmit={(e) => { e.preventDefault(); addBlock(); }}>
                        <input placeholder="＋ add a point…" value={adding} onChange={(e) => setAdding(e.target.value)}
                          onKeyDown={(e) => e.key === "Escape" && (e.stopPropagation(), setAdding(""))} aria-label="Add a point" />
                      </form>
                    )}
                    {page.links?.length > 0 && (
                      <div className="kbx-refs">
                        links to {page.links.map((l) => (
                          <button key={l} type="button" className="kbx-link" onClick={() => open(l)}>{l}</button>
                        ))}
                      </div>
                    )}
                    {page.backlinks?.length > 0 && (
                      <div className="kbx-refs">
                        linked from {page.backlinks.map((l) => (
                          <button key={l} type="button" className="kbx-link" onClick={() => open(l)}>{l}</button>
                        ))}
                      </div>
                    )}
                  </>
                )}
              </article>
            ) : (
              <ul className="kbx-list" id="kbx-matches" ref={listRef} role="listbox" aria-label="Pages">
                {matches.map((n, i) => (
                  <li key={n.id} draggable={n.kind !== "tool"} onDragStart={n.kind !== "tool" ? drag(n.label) : undefined} data-i={i}>
                    <button type="button" onClick={() => pick(n)} onMouseEnter={() => setCursor(i)}
                      className={`${n.kind || (n.journal ? "journal" : n.jarvis ? "jarvis" : "")}${i === cursor ? " cur" : ""}`}
                      role="option" aria-selected={i === cursor}>
                      <span>{n.label}</span>
                      <i>{n.kind === "tool" ? `tool · ${n.family}` : n.kind === "skill" ? `skill · ${skillState(n)}`
                        : `${n.blocks}${n.updated ? ` · ${ago(n.updated)}` : ""}`}</i>
                    </button>
                  </li>
                ))}
                {!matches.length && <li className="kbx-dim">No pages match.</li>}
              </ul>
            )}
          </aside>
        </div>
      </div>
    </div>
  );
}
