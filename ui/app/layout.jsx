import { Chakra_Petch, JetBrains_Mono } from "next/font/google";
import "./globals.css";

// Self-hosted at build time by next/font (no runtime requests to Google).
const mono = JetBrains_Mono({ subsets: ["latin"], variable: "--font-mono", display: "swap" });
const display = Chakra_Petch({
  subsets: ["latin"],
  weight: ["300", "500", "600"],
  variable: "--font-display",
  display: "swap",
});

export const metadata = { title: "JARVIS" };
export const viewport = { themeColor: "#04070a", width: "device-width", initialScale: 1 };

export default function RootLayout({ children }) {
  return (
    <html lang="en" className={`${mono.variable} ${display.variable}`}>
      <body>{children}</body>
    </html>
  );
}
