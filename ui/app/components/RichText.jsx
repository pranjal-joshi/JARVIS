"use client";

import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

// Replies are mostly spoken-style prose; markdown (code, steps, tables) renders only when present.
// Raw HTML is never rendered and unsafe link schemes are dropped (react-markdown defaults).
const RICH = /(^|\n)\s*(```|#{1,6}\s|[-*+]\s|\d+[.)]\s|\|.*\|)|\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\)/;

const components = {
  a: ({ node, ...p }) => <a {...p} target="_blank" rel="noopener noreferrer" />,
  table: ({ node, ...p }) => <div className="rt-table"><table {...p} /></div>,
};

export default function RichText({ text }) {
  if (!RICH.test(text)) return <>{text}</>;
  return (
    <div className="rt">
      <Markdown remarkPlugins={[remarkGfm]} components={components}>{text}</Markdown>
    </div>
  );
}
