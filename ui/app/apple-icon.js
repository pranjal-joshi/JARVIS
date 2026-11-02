import { ImageResponse } from "next/og";

// Home-screen icon (iOS/Android "Add to Home Screen"): the tab icon (icon.svg) on a full dark square,
// since the phone rounds the corners itself.
export const size = { width: 180, height: 180 };
export const contentType = "image/png";

export default function AppleIcon() {
  return new ImageResponse(
    (
      <div style={{ width: "100%", height: "100%", display: "flex", background: "#04070a" }}>
        <svg width="180" height="180" viewBox="0 0 64 64">
          <circle cx="32" cy="32" r="22" fill="none" stroke="#7fe7ff" strokeWidth="3" />
          <circle cx="32" cy="32" r="14.5" fill="none" stroke="#7fe7ff" strokeWidth="5" opacity="0.85"
                  strokeDasharray="22.8 7.6" transform="rotate(-90 32 32)" />
          <circle cx="32" cy="32" r="8" fill="#bff4ff" opacity="0.6" />
          <circle cx="32" cy="32" r="4.5" fill="#eafcff" />
        </svg>
      </div>
    ),
    size,
  );
}
