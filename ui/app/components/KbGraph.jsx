"use client";

// 3D knowledge-graph of the Logseq KB. Loaded client-only (next/dynamic, ssr: false)
// from Hud.jsx. Pages touched by JARVIS light up: reads cyan, writes amber.
import { useEffect, useRef } from "react";
import ForceGraph3D from "3d-force-graph";
import { AdditiveBlending, CanvasTexture, Color, FogExp2, Sprite, SpriteMaterial } from "three";

const C = {
  page: "#9fd8ff",
  jarvis: "#7fe7ff",
  journal: "#b48bff",
  read: "#7fe7ff",
  write: "#ffb347",
  link: "rgba(127,231,255,0.16)",
};
const LIT_MS = 9000; // a touched page stays lit this long, then fades to ambient

const esc = (s) =>
  String(s).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);

let glowTexture;
function glow() {
  if (glowTexture) return glowTexture;
  const cv = document.createElement("canvas");
  cv.width = cv.height = 64;
  const g = cv.getContext("2d");
  const grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  grad.addColorStop(0, "rgba(255,255,255,1)");
  grad.addColorStop(0.18, "rgba(255,255,255,0.85)");
  grad.addColorStop(0.45, "rgba(255,255,255,0.22)");
  grad.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = grad;
  g.fillRect(0, 0, 64, 64);
  glowTexture = new CanvasTexture(cv);
  return glowTexture;
}

const baseColor = (n) => (n.journal ? C.journal : n.jarvis ? C.jarvis : C.page);
const baseSize = (n) => 10 + Math.sqrt(n.blocks || 0) * 3.5;

export default function KbGraph({ pulse, active, version, onStats }) {
  const host = useRef(null);
  const api = useRef(null); // { graph, lit, reduced, flyTo, refreshLinks, load }

  // mount the renderer once
  useEffect(() => {
    const el = host.current;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const narrow = window.matchMedia("(max-width: 1000px)").matches;
    const lit = new Map(); // id -> { kind, at }
    let stopped = false;

    const graph = new ForceGraph3D(el, {
      controlType: "orbit",
      rendererConfig: { alpha: true, antialias: true, powerPreference: "low-power" },
    })
      .backgroundColor("rgba(0,0,0,0)")
      .showNavInfo(false)
      .enableNodeDrag(false)
      .nodeLabel((n) => `<div class="kb-tip"><b>${esc(n.label)}</b>${n.blocks ? `<span>${n.blocks} blocks</span>` : ""}</div>`)
      .nodeThreeObject((n) => {
        const mat = new SpriteMaterial({
          map: glow(),
          color: new Color(baseColor(n)),
          blending: AdditiveBlending,
          transparent: true,
          depthWrite: false,
          opacity: 0.6,
        });
        const s = new Sprite(mat);
        const sz = baseSize(n);
        s.scale.set(sz, sz, 1);
        n.__glow = s;
        return s;
      })
      .linkColor((l) => {
        const k = litKind(l);
        return k ? (k === "write" ? "rgba(255,179,71,0.75)" : "rgba(127,231,255,0.7)") : C.link;
      })
      .linkWidth((l) => (litKind(l) ? 0.6 : 0))
      .linkDirectionalParticles((l) => (litKind(l) && !reduced ? 3 : 0))
      .linkDirectionalParticleSpeed(0.007)
      .linkDirectionalParticleWidth(1.4)
      .linkDirectionalParticleColor((l) => (litKind(l) === "write" ? C.write : C.read));

    function litKind(l) {
      const s = typeof l.source === "object" ? l.source.id : l.source;
      const t = typeof l.target === "object" ? l.target.id : l.target;
      const a = lit.get(s);
      const b = lit.get(t);
      if (!a && !b) return null;
      return a?.kind === "write" || b?.kind === "write" ? "write" : "read";
    }

    graph.renderer().setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    graph.scene().fog = new FogExp2(0x04070a, 0.0012);
    // compact layout: small KB, small panel
    graph.d3Force("charge").strength(-28).distanceMax(90);
    graph.d3Force("link").distance(26);
    if (reduced) graph.warmupTicks(150).cooldownTicks(0); // static layout, computed up front
    else graph.cooldownTime(6000).d3VelocityDecay(0.3);

    const controls = graph.controls();
    controls.autoRotate = !reduced;
    controls.autoRotateSpeed = 0.35;
    controls.enableDamping = true;
    if (narrow) controls.enabled = false; // let touch scroll the stacked page

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
        let op = 0.55;
        if (L) {
          const t = (now - L.at) / 1000;
          if (now - L.at > LIT_MS) { lit.delete(n.id); expired = true; continue; }
          const fade = Math.min(1, (LIT_MS - (now - L.at)) / 1500);
          target = L.kind === "write" ? C.write : C.read;
          const beat = reduced ? 0 : L.kind === "write" ? Math.abs(Math.sin(t * 5)) * 0.5 : Math.sin(t * 3) * 0.2;
          scale = sz * (1 + (1.1 + beat) * fade);
          op = 0.55 + 0.45 * fade;
        } else if (!reduced) {
          op = 0.5 + 0.08 * Math.sin(now / 900 + sz);
        }
        s.material.color.lerp(tmp.set(target), reduced ? 1 : 0.12);
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
  }, [onStats]);

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

  return <div ref={host} className="kb-canvas" />;
}
