"use client";

// 3D knowledge-graph of the Logseq KB. Loaded client-only (next/dynamic, ssr: false)
// from Hud.jsx. Pages touched by JARVIS light up by tool kind: reads cyan, tasks green, writes amber,
// erases red, blocked/failed pink.
import { useEffect, useRef } from "react";
import ForceGraph3D from "3d-force-graph";
import { AdditiveBlending, CanvasTexture, Color, FogExp2, Group, Sprite, SpriteMaterial } from "three";
import { CSS2DObject, CSS2DRenderer } from "three/addons/renderers/CSS2DRenderer.js";

const C = {
  page: "#9fd8ff",
  jarvis: "#7fe7ff",
  journal: "#b48bff",
  read: "#7fe7ff",
  tasks: "#6dffa8",
  write: "#ffb347",
  erase: "#ff6b6b",
  fault: "#ff5c9a",
  link: "rgba(127,231,255,0.16)",
  select: "#eafcff",
};
const RANK = ["read", "tasks", "write", "erase", "fault"];
const LIT_MS = 9000; // a touched page stays lit this long, then fades to ambient

const esc = (s) =>
  String(s).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);

// Soft halo around each node.
let glowTexture;
function glow() {
  if (glowTexture) return glowTexture;
  const cv = document.createElement("canvas");
  cv.width = cv.height = 128;
  const g = cv.getContext("2d");
  const grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  grad.addColorStop(0, "rgba(255,255,255,0.55)");
  grad.addColorStop(0.3, "rgba(255,255,255,0.16)");
  grad.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = grad;
  g.fillRect(0, 0, 128, 128);
  glowTexture = new CanvasTexture(cv);
  return glowTexture;
}

// Crisp node core: solid disc with a thin bright rim, drawn at high resolution so it stays sharp.
let coreTexture;
function core() {
  if (coreTexture) return coreTexture;
  const cv = document.createElement("canvas");
  cv.width = cv.height = 128;
  const g = cv.getContext("2d");
  g.fillStyle = "rgba(255,255,255,0.9)";
  g.beginPath();
  g.arc(64, 64, 40, 0, Math.PI * 2);
  g.fill();
  g.strokeStyle = "rgba(255,255,255,1)";
  g.lineWidth = 6;
  g.beginPath();
  g.arc(64, 64, 54, 0, Math.PI * 2);
  g.stroke();
  coreTexture = new CanvasTexture(cv);
  coreTexture.anisotropy = 4;
  return coreTexture;
}

const baseColor = (n) => (n.journal ? C.journal : n.jarvis ? C.jarvis : C.page);
const baseSize = (n) => 10 + Math.sqrt(n.blocks || 0) * 3.5;

export default function KbGraph({ pulse, active, version, onStats, interactive = false, selected = null, onPick }) {
  const host = useRef(null);
  // interactive (explorer) mode: click picks a page, nodes drag, the camera is the user's
  const live = useRef({ selected, onPick });
  live.current = { selected, onPick };
  const api = useRef(null); // { graph, lit, reduced, flyTo, refreshLinks, load }

  // mount the renderer once
  useEffect(() => {
    const el = host.current;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const narrow = window.matchMedia("(max-width: 1000px)").matches;
    const lit = new Map(); // id -> { kind, at }
    let stopped = false;

    // Page names as real DOM text (CSS2D) so they render sharp at any zoom.
    const labels = new CSS2DRenderer();
    labels.domElement.className = "kb-labels";
    const graph = new ForceGraph3D(el, {
      controlType: "orbit",
      extraRenderers: [labels],
      rendererConfig: { alpha: true, antialias: true, powerPreference: "low-power" },
    })
      .backgroundColor("rgba(0,0,0,0)")
      .showNavInfo(false)
      .enableNodeDrag(interactive)
      .nodeLabel((n) => `<div class="kb-tip"><b>${esc(n.label)}</b>${n.blocks ? `<span>${n.blocks} blocks</span>` : ""}</div>`)
      .nodeThreeObject((n) => {
        const sprite = (map, opacity, additive) => new Sprite(new SpriteMaterial({
          map, color: new Color(baseColor(n)), transparent: true, depthWrite: false, opacity,
          ...(additive ? { blending: AdditiveBlending } : {}),
        }));
        const sz = baseSize(n);
        const halo = sprite(glow(), 0.5, true);
        halo.scale.set(sz, sz, 1);
        const dot = sprite(core(), 0.95, false);
        dot.scale.set(sz * 0.32, sz * 0.32, 1);
        const tag = document.createElement("div");
        tag.className = "kb-label";
        tag.textContent = n.label;
        const label = new CSS2DObject(tag);
        label.position.set(0, -sz * 0.32, 0);
        const group = new Group();
        group.add(halo, dot, label);
        n.__glow = halo;
        n.__core = dot;
        n.__tag = tag;
        return group;
      })
      .linkColor((l) => {
        const k = litKind(l);
        return k ? C[k] : C.link;
      })
      .linkWidth((l) => (litKind(l) ? 0.8 : 0))
      .linkOpacity(0.55)
      .linkDirectionalParticles((l) => (litKind(l) && !reduced ? 3 : 0))
      .linkDirectionalParticleSpeed(0.007)
      .linkDirectionalParticleWidth(1.4)
      .linkDirectionalParticleColor((l) => C[litKind(l)] || C.read);

    function litKind(l) {
      const s = typeof l.source === "object" ? l.source.id : l.source;
      const t = typeof l.target === "object" ? l.target.id : l.target;
      const a = lit.get(s);
      const b = lit.get(t);
      if (!a && !b) return null;
      if (!a || !b) return (a || b).kind;
      return RANK.indexOf(a.kind) >= RANK.indexOf(b.kind) ? a.kind : b.kind; // the stronger kind wins
    }

    graph.renderer().setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    graph.scene().fog = new FogExp2(0x04070a, 0.0006);
    // compact layout: small KB, small panel
    graph.d3Force("charge").strength(interactive ? -60 : -28).distanceMax(interactive ? 180 : 90);
    graph.d3Force("link").distance(interactive ? 40 : 26);
    if (interactive) {
      graph.onNodeClick((n) => { live.current.onPick?.(n); flyTo([n.id], 900); controls.autoRotate = false; })
        .onNodeHover((n) => { el.style.cursor = n ? "pointer" : "grab"; })
        .onBackgroundClick(() => live.current.onPick?.(null));
    }
    if (reduced) graph.warmupTicks(150).cooldownTicks(0); // static layout, computed up front
    else graph.cooldownTime(6000).d3VelocityDecay(0.3);

    const controls = graph.controls();
    controls.autoRotate = !reduced;
    controls.autoRotateSpeed = 0.35;
    controls.enableDamping = true;
    if (narrow && !interactive) controls.enabled = false; // let touch scroll the stacked page
    if (interactive) controls.addEventListener("start", () => { controls.autoRotate = false; state.busy = true; });

    // refit once the layout settles, unless we're focused on touched pages
    const state = { busy: false };
    graph.onEngineStop(() => { if (!state.busy && !stopped) graph.zoomToFit(reduced ? 0 : 900, 18); });

    const size = () => graph.width(el.clientWidth).height(el.clientHeight);
    const ro = new ResizeObserver(size);
    ro.observe(el);
    size();

    const refreshLinks = () => {
      graph.linkColor(graph.linkColor()).linkWidth(graph.linkWidth()).linkDirectionalParticles(graph.linkDirectionalParticles());
    };

    const flyTo = (ids, ms) => {
      const nodes = graph.graphData().nodes.filter((n) => ids.includes(n.id) && Number.isFinite(n.x));
      if (!nodes.length) return false;
      const c = { x: 0, y: 0, z: 0 };
      nodes.forEach((n) => { c.x += n.x / nodes.length; c.y += n.y / nodes.length; c.z += n.z / nodes.length; });
      const spread = Math.max(...nodes.map((n) => Math.hypot(n.x - c.x, n.y - c.y, n.z - c.z)), 0);
      const dist = 90 + spread * 1.6;
      graph.cameraPosition({ x: c.x + dist * 0.35, y: c.y + dist * 0.2, z: c.z + dist }, c, reduced ? 0 : ms);
      return true;
    };

    // node pulse / colour loop (pauses with the renderer)
    const tmp = new Color();
    let raf;
    const tick = (now) => {
      const nodes = graph.graphData().nodes;
      let expired = false;
      for (const n of nodes) {
        const s = n.__glow;
        if (!s) continue;
        const L = lit.get(n.id);
        const sz = baseSize(n);
        let target = baseColor(n);
        let scale = sz;
        let op = 0.45;
        const picked = n.id === live.current.selected;
        n.__tag?.classList.toggle("lit", !!L);
        for (const k of RANK) n.__tag?.classList.toggle(k, L?.kind === k);
        if (L) {
          const t = (now - L.at) / 1000;
          if (now - L.at > LIT_MS) { lit.delete(n.id); expired = true; continue; }
          const fade = Math.min(1, (LIT_MS - (now - L.at)) / 1500);
          target = C[L.kind] || C.read;
          const strong = L.kind !== "read" && L.kind !== "tasks";
          const beat = reduced ? 0 : strong ? Math.abs(Math.sin(t * 5)) * 0.5 : Math.sin(t * 3) * 0.2;
          scale = sz * (1 + (1.1 + beat) * fade);
          op = 0.55 + 0.45 * fade;
        } else if (picked) {
          target = C.select;
          scale = sz * (1.9 + (reduced ? 0 : 0.15 * Math.sin(now / 240)));
          op = 0.95;
        } else if (!reduced) {
          op = 0.5 + 0.08 * Math.sin(now / 900 + sz);
        }
        n.__tag?.classList.toggle("picked", picked);
        s.material.color.lerp(tmp.set(target), reduced ? 1 : 0.12);
        n.__core?.material.color.copy(s.material.color);
        s.material.opacity += (op - s.material.opacity) * (reduced ? 1 : 0.15);
        const cur = s.scale.x + (scale - s.scale.x) * (reduced ? 1 : 0.15);
        s.scale.set(cur, cur, 1);
      }
      if (expired) refreshLinks();
      raf = requestAnimationFrame(tick);
    };
    const run = () => { if (!raf && !stopped) raf = requestAnimationFrame(tick); graph.resumeAnimation(); };
    const pause = () => { cancelAnimationFrame(raf); raf = 0; graph.pauseAnimation(); };
    run();

    // pause rendering when off-screen or the tab is hidden
    let onScreen = true;
    const sync = () => (onScreen && !document.hidden ? run() : pause());
    const io = new IntersectionObserver(([e]) => { onScreen = e.isIntersecting; sync(); });
    io.observe(el);
    document.addEventListener("visibilitychange", sync);

    const load = async () => {
      try {
        const r = await fetch("/api/hub/kb/graph", { cache: "no-store" });
        if (!r.ok || stopped) return;
        const { nodes = [], edges = [] } = await r.json();
        const prev = new Map(graph.graphData().nodes.map((n) => [n.id, n]));
        const merged = nodes.map((n) => Object.assign(prev.get(n.id) || {}, n));
        graph.graphData({ nodes: merged, links: edges.map((e) => ({ source: e.source, target: e.target })) });
        onStats?.({ pages: nodes.length, links: edges.length });
        if (!prev.size) setTimeout(() => !stopped && graph.zoomToFit(reduced ? 0 : 1200, 18), reduced ? 50 : 900);
      } catch {
        /* hub offline: keep whatever is shown */
      }
    };

    api.current = { graph, lit, reduced, flyTo, refreshLinks, load, controls, state };
    load();

    return () => {
      stopped = true;
      pause();
      io.disconnect();
      ro.disconnect();
      document.removeEventListener("visibilitychange", sync);
      graph._destructor();
      el.replaceChildren();
      api.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onStats, interactive]);

  // explorer: a page picked from the list (or a link) flies the camera to it too
  useEffect(() => {
    const a = api.current;
    if (!a || !interactive || !selected) return;
    a.controls.autoRotate = false;
    a.state.busy = true;
    a.flyTo([selected], 900);
  }, [selected, interactive]);

  // refetch after writes so new pages appear
  useEffect(() => {
    if (version) api.current?.load();
  }, [version]);

  // light up touched pages and fly the camera to them
  useEffect(() => {
    const a = api.current;
    if (!a || !pulse) return;
    const ids = [...new Set(pulse.pages.map((p) => String(p).toLowerCase().trim()).filter(Boolean))];
    const now = performance.now();
    ids.forEach((id) => a.lit.set(id, { kind: pulse.kind, at: now }));
    a.refreshLinks();
    a.controls.autoRotate = false;
    a.state.busy = true;
    if (!ids.length) return;
    // a page written just now may only exist after the refetch: retry the fly-to briefly
    let tries = 0;
    let timer;
    const go = () => {
      if (a.flyTo(ids, 1400) || ++tries > 6) return;
      timer = setTimeout(go, 400);
    };
    go();
    return () => clearTimeout(timer);
  }, [pulse]);

  // settle back to ambient
  useEffect(() => {
    const a = api.current;
    if (!a || active) return;
    a.state.busy = false;
    a.graph.zoomToFit(a.reduced ? 0 : 1600, 18);
    a.controls.autoRotate = !a.reduced;
  }, [active]);

  return <div ref={host} className={`kb-canvas${interactive ? " interactive" : ""}`} />;
}
