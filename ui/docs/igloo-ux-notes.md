# igloo.inc: UX notes for the JARVIS HUD

Studied 2026-11-01 (read-only: rendered in the browser, inspected the DOM, computed
styles and the public JS bundle for motion parameters). Principles only; no assets,
code, fonts or copy were taken.

## What the site is

- A single full-viewport WebGL scene (Svelte shell + three.js-style renderer + GSAP).
  The DOM is just `#app > #webgl`; all UI text is drawn in the scene or as thin
  overlays. `html, body { overflow: hidden; touch-action: none }`: scroll is
  captured and converted into a timeline position (scroll-scrubbed camera), not
  native page scroll.
- Background is a flat cool grey (`--bgColor: #A0A5B1`) that the loader and the 3D
  fog both share, so the hand-off from loader to scene is seamless.

## Load sequence (fluency)

1. Instant flat background in the final scene colour (no white flash).
2. A tiny ASCII loader: a 10-char string of `- = +` cycling through `content`
   keyframes over 5 s, 17 px bold monospace, soft white text-shadow
   `0 0 5px rgba(255,255,255,.4)`. Minimal, typographic, on-brand.
3. The 3D app is code-split (`import("./App3D-*.js")`) and only when it reports
   `ready` does the loader fade out: text fades in **250 ms**, the loader layer in
   **750 ms**, both on a **cubic in-out** (`t<.5 ? 4t³ : …`).
4. Scene intro is a choreographed timeline of overlapping tweens (not sequential):
   wireframe "cage" lines appear first (glow value 0 -> 0.4 in 0.1 s, then decays over
   3 s), the solid model resolves at +1.1 s over 2.25 s, the ground fills in, the
   camera dollies for ~7 s with FOV 22 -> 30. Overlap is the key: every new layer
   starts before the previous one finishes.
5. UI text arrives last, with a **decode / scramble effect**: glyphs are random
   characters that resolve left-to-right into the real words over a few hundred ms.

## Motion language (concrete values)

- GSAP defaults: `ease: power2.inOut`, `duration: 0.6 s`, `overwrite: auto`.
- Custom in-out curves (as CSS cubic-bezier equivalents):
  - `inOut1` = `cubic-bezier(0.5, 0, 0.1, 1)`
  - `inOut2` = `cubic-bezier(0.56, 0, 0, 1)`
  - `inOut3` = `cubic-bezier(0.6, 0, 0, 1)`
  - `inOut4` = `cubic-bezier(0.4, 0, -0.06, 1)` (very slight anticipation)
  - `igloo_ease_1` = `cubic-bezier(0.662, 0.073, 0.047, 1)` (slow start, long glide)
  - `entry_ease_2` = `cubic-bezier(0.388, 0.082, 0.924, 0.862)`
  These are all "slow-in, decisive, long soft landing" curves: little motion for
  the first ~40 %, then a fast middle, then a long settle.
- Durations cluster in three bands:
  - micro: 0.075, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35 s (UI feedback, flashes)
  - meso: 0.4, 0.5, 0.6, 0.7, 0.75, 1, 1.25, 1.45, 1.5 s (panel/text transitions)
  - macro: 2-7.5 s, up to 14 s (camera, scene, ambient loops)
- Also used: `power3.out`, `power4.out` for entrances; `sine.inOut` for ambient
  loops (breathing, drifting particles); `power2.in`/`power3.in` for exits.
- Stagger is used for groups (GSAP `stagger`), typically small offsets.
- 60 fps ticker with a delta clamp; DPR capped (about 1.15 on <=2x screens) to keep
  frame time smooth: they trade resolution for fluency.

## Typography

- One family: a monospaced grotesque (Plex Mono, Regular + Medium), small sizes,
  uppercase-free sentence case, short lines, right-aligned blocks on the right edge.
- Labels use code-like prefixes: `//`, `//////`, numbered markers on 3D objects.
- `text-rendering: optimizeLegibility; -webkit-font-smoothing: antialiased`.
- Text is white with a faint glow, never fully opaque blocks of copy.

## Colour, texture, depth

- Near-monochrome cool palette: greys and ice blues (`#A0A5B1`, `#d1d6e3`,
  `#c9d0df`, `#83a1c5`, `#b5d5ff`, deep `#09121f`/`#222b42`), a single warm accent
  family (`#cda05e`, `#ab8349`) used sparingly.
- Post-processing: bloom on emissive edges, chromatic aberration, dithering to
  avoid banding, a 3D LUT for grading, film grain (present in the shader).
- Depth through atmospheric fog, thin bright wireframe lines over soft volumes, and
  emissive seams (light leaking between blocks).

## Interaction and sound

- Raycast hover on 3D objects with hover/grab cursors; drag to rotate.
- A full sound design: ambient beds (music, room, wind) that fade in from volume 0,
  UI sounds (`ui-short`, `ui-long`, beeps rate-limited with `minTimeBetweenPlays:
  0.4 s`), and enter/leave sounds for projects. Sound is **off by default** with a
  visible "Sound: Off" toggle.

## What translates to the HUD

| igloo principle | HUD application |
| --- | --- |
| Default motion = 0.6 s power2-in-out, custom "slow-in long-settle" curves | CSS tokens `--ease-out`, `--ease-in-out`, `--ease-glide` and `--dur-micro/short/med/long` |
| Loader colour == scene colour; overlapping choreographed intro | Background is final from frame 1; header, panels, orb and input enter with an overlapping stagger (80-120 ms offsets, 700-900 ms each) |
| Exits faster than entrances (`power*.in`, shorter) | Approval cards leave in ~280 ms, enter in ~600 ms |
| Ambient loops on `sine.inOut` | Orb breathing and state glow transitions use sine-like smoothing; colours cross-fade between states instead of snapping |
| Decode/scramble text reveal | Optional short decode on the orb state label (skipped under reduced motion) |
| Monospace, small, glow, `//` prefixes | JetBrains Mono for body (not their font), Chakra Petch for the display/labels, `//` prefixes on panel titles |
| Bloom, grain, dithering | Subtle SVG-noise grain overlay, soft bloom around the orb, vignette kept |
| Thin bright lines over soft volumes | Orb gets thin tick rings and a soft core, panels get hairline corner brackets |
| DPR cap for fluency | Orb canvas DPR capped at 2 (already) and colour/level smoothed per frame |
| Sound off by default, rate-limited UI sounds | Not adopted now (would need assets); noted as a future idea |

Not adopted: scroll-hijacking (our HUD is an app, not a story), hover-raycast
3D interaction, and audio beds.
