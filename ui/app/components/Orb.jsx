"use client";

import { useEffect, useRef } from "react";

// Audio-reactive core orb on a 2D canvas. Phase 9 swaps this for an R3F/GLSL
// version; the props (state, analyser) stay the same.
const COLORS = {
  idle: [127, 231, 255],
  listening: [127, 231, 255],
  thinking: [255, 179, 71],
  speaking: [160, 240, 255],
  error: [255, 92, 92],
};

// per-state motion character: ring spin speed and breathing amplitude/rate
const ENERGY = {
  idle: { spin: 1, breathe: 0.03, rate: 1.5 },
  listening: { spin: 1.4, breathe: 0.03, rate: 2 },
  thinking: { spin: 2.6, breathe: 0.06, rate: 6 },
  speaking: { spin: 1.6, breathe: 0.02, rate: 2 },
  error: { spin: 0.4, breathe: 0.02, rate: 1 },
};

const TAU = Math.PI * 2;
// slow-in, long soft landing (approximation of cubic-bezier(0.66, 0.07, 0.05, 1))
const glide = (x) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2);

export default function Orb({ state = "idle", analyser, size = 280 }) {
  const canvas = useRef(null);
  const live = useRef({ state, analyser });
  live.current = { state, analyser };

  useEffect(() => {
    const c = canvas.current;
    const ctx = c.getContext("2d");
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    c.width = size * dpr;
    c.height = size * dpr;
    ctx.scale(dpr, dpr);
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const bins = new Uint8Array(128);
    const cx = size / 2;
    const base = size * 0.2;

    // smoothed values so state changes cross-fade instead of snapping
    const col = [...COLORS[live.current.state] || COLORS.idle];
    let level = 0;
    let spin = 1;
    let breathe = 0.03;
    let rate = 1.5;
    let phase = 0;
    let rot = 0;
    let errorFlash = 0;
    let prevState = live.current.state;
    const ripples = [];
    let nextRipple = 0;
    let raf;
    let last = performance.now();
    const born = last;

    const draw = (now) => {
      const { state: s, analyser: a } = live.current;
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      const move = reduced ? 0 : dt;
      const k = (speed) => (reduced ? 1 : 1 - Math.exp(-dt * speed));

      let raw = 0;
      if (a) {
        a.getByteFrequencyData(bins);
        raw = bins.reduce((x, y) => x + y, 0) / bins.length / 255;
      } else {
        bins.fill(0);
      }
      // fast attack, slow release
      level += (raw - level) * (raw > level ? k(18) : k(5));

      const target = COLORS[s] || COLORS.idle;
      for (let i = 0; i < 3; i++) col[i] += (target[i] - col[i]) * k(5);
      const e = ENERGY[s] || ENERGY.idle;
      spin += (e.spin - spin) * k(3);
      breathe += (e.breathe - breathe) * k(3);
      rate += (e.rate - rate) * k(3);
      phase += move * rate;
      rot += move * spin;
      if (s !== prevState) {
        if (s === "error" && !reduced) errorFlash = 1;
        prevState = s;
      }
      errorFlash = Math.max(0, errorFlash - move * 1.5);

      const intro = reduced ? 1 : glide(Math.min(1, (now - born) / 1600));
      const [r, g, b] = col.map(Math.round);
      const rgba = (al) => `rgba(${r},${g},${b},${Math.max(0, Math.min(1, al))})`;
      const flicker = s === "error" && !reduced ? 0.75 + 0.25 * Math.sin(now / 37) * Math.sin(now / 91) : 1;
      const radius = base * (1 + breathe * Math.sin(phase) + level * 0.55) * (0.85 + 0.15 * intro);

      ctx.clearRect(0, 0, size, size);
      ctx.globalCompositeOperation = "lighter";

      // soft bloom
      const glow = ctx.createRadialGradient(cx, cx, radius * 0.1, cx, cx, radius * 2.4);
      glow.addColorStop(0, rgba((0.55 + level * 0.3) * intro * flicker));
      glow.addColorStop(0.3, rgba(0.16 * intro));
      glow.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = glow;
      ctx.fillRect(0, 0, size, size);

      // outer tick ring (fine, slow counter-rotation)
      const tickR = size * 0.46;
      const ticks = 90;
      ctx.lineWidth = 1;
      for (let i = 0; i < Math.floor(ticks * intro); i++) {
        const ang = (i / ticks) * TAU - rot * 0.08;
        const long = i % 5 === 0;
        const len = long ? 7 : 3;
        ctx.strokeStyle = rgba(long ? 0.45 : 0.2);
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(ang) * tickR, cx + Math.sin(ang) * tickR);
        ctx.lineTo(cx + Math.cos(ang) * (tickR - len), cx + Math.sin(ang) * (tickR - len));
        ctx.stroke();
      }

      // rotating segmented rings (audio reactive), drawn on during the intro
      for (let ring = 0; ring < 3; ring++) {
        const rr = radius * (1.35 + ring * 0.32);
        const segs = 24 + ring * 12;
        const dir = ring % 2 ? -1 : 1;
        const shown = Math.floor(segs * Math.min(1, intro * 1.4 - ring * 0.15));
        for (let i = 0; i < shown; i++) {
          if ((i + ring) % 3 === 0) continue;
          const amp = bins[(i * 3 + ring * 7) % bins.length] / 255;
          const a0 = (i / segs) * TAU + rot * dir * (0.3 + ring * 0.15);
          ctx.strokeStyle = rgba((0.22 + amp * 0.6) * flicker);
          ctx.lineWidth = 1.25 + amp * 3;
          ctx.beginPath();
          ctx.arc(cx, cx, rr + amp * 8, a0, a0 + (TAU / segs) * 0.6);
          ctx.stroke();
        }
      }

      // thinking: two comet arcs orbiting the outer ring
      if (s === "thinking" || spin > 2) {
        const w = Math.min(1, (spin - 1) / 1.6);
        const orbitR = radius * 2.2;
        for (let j = 0; j < 2; j++) {
          const head = rot * 1.4 + j * Math.PI;
          for (let q = 0; q < 14; q++) {
            ctx.strokeStyle = rgba(w * 0.7 * (1 - q / 14));
            ctx.lineWidth = 2 - q * 0.1;
            ctx.beginPath();
            ctx.arc(cx, cx, orbitR, head - (q + 1) * 0.045, head - q * 0.045);
            ctx.stroke();
          }
        }
      }

      // listening: ripples expand from the core, louder voice = stronger ripple
      if (!reduced && s === "listening" && now > nextRipple) {
        ripples.push({ t: 0, strength: 0.35 + level });
        nextRipple = now + 900 - level * 500;
      }
      for (let i = ripples.length - 1; i >= 0; i--) {
        const rp = ripples[i];
        rp.t += dt / 1.6;
        if (rp.t >= 1) {
          ripples.splice(i, 1);
          continue;
        }
        const ease = 1 - Math.pow(1 - rp.t, 3);
        ctx.strokeStyle = rgba((1 - rp.t) * 0.5 * rp.strength);
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(cx, cx, radius * (0.6 + ease * 1.9), 0, TAU);
        ctx.stroke();
      }

      // speaking: radial spectrum spikes around the core
      if (a && s === "speaking") {
        const n = 64;
        for (let i = 0; i < n; i++) {
          const amp = bins[i % bins.length] / 255;
          if (amp < 0.04) continue;
          const ang = (i / n) * TAU + rot * 0.2;
          const r0 = radius * 0.95;
          const r1 = r0 + amp * radius * 0.6;
          ctx.strokeStyle = rgba(0.25 + amp * 0.6);
          ctx.lineWidth = 1.5;
          ctx.beginPath();
          ctx.moveTo(cx + Math.cos(ang) * r0, cx + Math.sin(ang) * r0);
          ctx.lineTo(cx + Math.cos(ang) * r1, cx + Math.sin(ang) * r1);
          ctx.stroke();
        }
      }

      // core: lit sphere with a hot centre and a thin rim
      ctx.globalCompositeOperation = "source-over";
      const coreR = radius * 0.55;
      const core = ctx.createRadialGradient(cx - coreR * 0.25, cx - coreR * 0.3, coreR * 0.05, cx, cx, coreR);
      core.addColorStop(0, `rgba(255,255,255,${0.95 * intro})`);
      core.addColorStop(0.35, rgba(0.95 * intro * flicker));
      core.addColorStop(1, rgba(0.55 * intro * flicker));
      ctx.fillStyle = core;
      ctx.beginPath();
      ctx.arc(cx, cx, coreR, 0, TAU);
      ctx.fill();
      ctx.strokeStyle = rgba(0.6 * intro);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.arc(cx, cx, coreR + 3, 0, TAU);
      ctx.stroke();

      if (errorFlash > 0) {
        ctx.fillStyle = `rgba(255,92,92,${errorFlash * 0.25})`;
        ctx.beginPath();
        ctx.arc(cx, cx, size * 0.48, 0, TAU);
        ctx.fill();
      }

      raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, [size]);

  return (
    <canvas
      ref={canvas}
      role="img"
      aria-label={`JARVIS is ${state}`}
      style={{ width: size, maxWidth: "100%", height: "auto", aspectRatio: "1 / 1", display: "block", margin: "0 auto" }}
    />
  );
}
