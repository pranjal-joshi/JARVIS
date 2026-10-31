import "./globals.css";

export const metadata = { title: "JARVIS" };
export const viewport = { themeColor: "#04070a", width: "device-width", initialScale: 1 };

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
