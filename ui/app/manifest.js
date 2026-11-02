export default function manifest() {
  return {
    name: "JARVIS",
    short_name: "JARVIS",
    start_url: "/",
    display: "standalone",
    background_color: "#04070a",
    theme_color: "#04070a",
    icons: [
      { src: "/icon.svg", sizes: "any", type: "image/svg+xml" },
      { src: "/apple-icon", sizes: "180x180", type: "image/png" },
    ],
  };
}
