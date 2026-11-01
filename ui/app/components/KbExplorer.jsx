"use client";

// Full-screen knowledge-graph explorer: fly around the graph, click a page to read it, follow its
// links, and add pages to the chat as context (button, or drag the page onto the chat box).

import { useEffect, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";

const KbGraph = dynamic(() => import("./KbGraph"), {
  ssr: false,
  loading: () => <div className="kb-canvas kb-loading">initialising graph…</div>,
});

export const PAGE_MIME = "application/x-jarvis-page";
const api = (path) => fetch(`/api/hub/${path}`, { cache: "no-store" });

// [[links]] in a block become buttons that open that page
function Block({ text, onOpen }) {
  const parts = text.split(/(\[\[[^\]]+\]\])/g);
  return (
    <li>
      {parts.map((p, i) => {
        const m = p.match(/^\[\[([^\]]+)\]\]$/);
        return m
          ? <button key={i} type="button" className="kbx-link" onClick={() => onOpen(m[1])}>{m[1]}</button>
          : <span key={i}>{p}</span>;
      })}
    </li>
  );
}

export default function KbExplorer({ version, context, onAdd, onClose }) {
  const [nodes, setNodes] = useState([]);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(null); // node id
  const [page, setPage] = useState(null); // { name, blocks, links, link } | { error }
  const searchRef = useRef(null);

  useEffect(() => {
    api("kb/graph").then((r) => (r.ok ? r.json() : { nodes: [] })).then((g) => setNodes(g.nodes || [])).catch(() => {});
  }, [version]);

  useEffect(() => {
    searchRef.current?.focus();
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const open = async (name) => {
    const id = String(name).toLowerCase();
    setSelected(id);
    setPage({ name, loading: true });
    const r = await api(`kb/page?name=${encodeURIComponent(name)}`).catch(() => null);
    setPage(r?.ok ? await r.json() : { name, error: "Not a page yet: only linked to." });
  };

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = nodes.filter((n) => n.blocks > 0 && (!q || n.label.toLowerCase().includes(q)));
    return list.sort((a, b) => (b.journal ? 0 : 1) - (a.journal ? 0 : 1) || b.blocks - a.blocks).slice(0, 40);
  }, [nodes, query]);

  const inContext = (name) => context.some((c) => c.name.toLowerCase() === String(name).toLowerCase());
  const add = (p) => p?.blocks && onAdd({ name: p.name, text: p.blocks.map((b) => `- ${b}`).join("\n") });
  const drag = (name) => (e) => {
    e.dataTransfer.setData(PAGE_MIME, name);
    e.dataTransfer.setData("text/plain", `[[${name}]]`);
    e.dataTransfer.effectAllowed = "copy";
  };

  return (
    <div className="kbx" role="dialog" aria-modal="true" aria-label="Knowledge graph explorer">
      <div className="kbx-frame">
        <header className="kbx-head">
          <h2>Knowledge graph <small>{nodes.filter((n) => n.blocks).length} pages · click to read · drag to chat</small></h2>
          <button type="button" className="kbx-close" onClick={onClose} title="Close (Esc)" aria-label="Close explorer">✕</button>
        </header>
        <div className="kbx-body">
          <div className="kbx-graph">
            <KbGraph interactive version={version} selected={selected}
              onPick={(n) => (n ? open(n.label) : (setSelected(null), setPage(null)))} />
            <div className="kbx-legend" aria-hidden="true">
              <span className="dot page" />page <span className="dot jarvis" />jarvis memory <span className="dot journal" />journal
            </div>
          </div>
          <aside className="kbx-side">
            <input ref={searchRef} className="kbx-search" placeholder="Find a page…" value={query}
              onChange={(e) => setQuery(e.target.value)} aria-label="Find a page" />
            {page ? (
              <article className="kbx-page" draggable onDragStart={drag(page.name)}>
                <div className="kbx-title">
                  <span className="grip" aria-hidden="true">⠿</span>
                  <h3>{page.name}</h3>
                  <button type="button" className="kbx-back" onClick={() => { setPage(null); setSelected(null); }}>back</button>
                </div>
                {page.loading && <p className="kbx-dim">reading…</p>}
                {page.error && <p className="kbx-dim">{page.error}</p>}
                {page.blocks && (
                  <>
                    <div className="kbx-actions">
                      <button type="button" className="kbx-btn primary" disabled={inContext(page.name)} onClick={() => add(page)}>
                        {inContext(page.name) ? "✓ in chat context" : "＋ add to chat"}
                      </button>
                      <a className="kbx-btn" href={page.link}>open in Logseq</a>
                    </div>
                    <ul className="kbx-blocks">
                      {page.blocks.map((b, i) => <Block key={i} text={b} onOpen={open} />)}
                      {!page.blocks.length && <li className="kbx-dim">Empty page.</li>}
                    </ul>
                    {page.links?.length > 0 && (
                      <div className="kbx-refs">
                        links to {page.links.map((l) => (
                          <button key={l} type="button" className="kbx-link" onClick={() => open(l)}>{l}</button>
                        ))}
                      </div>
                    )}
                  </>
                )}
              </article>
            ) : (
              <ul className="kbx-list">
                {matches.map((n) => (
                  <li key={n.id} draggable onDragStart={drag(n.label)}>
                    <button type="button" onClick={() => open(n.label)} className={n.journal ? "journal" : n.jarvis ? "jarvis" : ""}>
                      <span>{n.label}</span><i>{n.blocks}</i>
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
