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
    let raf;
    let t = 0;

    const draw = () => {
      const { state: s, analyser: a } = live.current;
      t += reduced ? 0 : 0.016;
      let level = 0;
      if (a) {
        a.getByteFrequencyData(bins);
        level = bins.reduce((x, y) => x + y, 0) / bins.length / 255;
      }
      const [r, g, b] = COLORS[s] || COLORS.idle;
      const cx = size / 2;
      const base = size * 0.22;
      const pulse = s === "thinking" ? 0.06 * Math.sin(t * 6) : 0.03 * Math.sin(t * 1.5);
      const radius = base * (1 + pulse + level * 0.6);

      ctx.clearRect(0, 0, size, size);
      const glow = ctx.createRadialGradient(cx, cx, radius * 0.2, cx, cx, radius * 2.2);
      glow.addColorStop(0, `rgba(${r},${g},${b},0.9)`);
      glow.addColorStop(0.35, `rgba(${r},${g},${b},0.25)`);
      glow.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(cx, cx, radius * 2.2, 0, Math.PI * 2);
      ctx.fill();

      // rotating segmented rings
      for (let ring = 0; ring < 3; ring++) {
        const rr = radius * (1.35 + ring * 0.32);
        const segs = 24 + ring * 12;
        const dir = ring % 2 ? -1 : 1;
        for (let i = 0; i < segs; i++) {
          if ((i + ring) % 3 === 0) continue;
          const amp = a ? bins[(i * 3 + ring * 7) % bins.length] / 255 : 0;
          const a0 = (i / segs) * Math.PI * 2 + t * dir * (0.3 + ring * 0.15);
          ctx.strokeStyle = `rgba(${r},${g},${b},${0.25 + amp * 0.6})`;
          ctx.lineWidth = 1.5 + amp * 3;
          ctx.beginPath();
          ctx.arc(cx, cx, rr + amp * 8, a0, a0 + (Math.PI * 2) / segs * 0.6);
          ctx.stroke();
        }
      }
      ctx.fillStyle = `rgba(${r},${g},${b},0.95)`;
      ctx.beginPath();
      ctx.arc(cx, cx, radius * 0.55, 0, Math.PI * 2);
      ctx.fill();
      raf = requestAnimationFrame(draw);
    };
    draw();
    return () => cancelAnimationFrame(raf);
  }, [size]);

  return (
    <canvas
      ref={canvas}
      aria-label={`JARVIS is ${state}`}
      style={{ width: size, height: size, display: "block", margin: "0 auto" }}
    />
  );
}
