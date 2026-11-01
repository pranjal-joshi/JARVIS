"""Logseq file-based graph adapter (markdown on disk). Reads everything; writes
only to the `jarvis/` namespace and the journal unless a caller explicitly
uses `append_to_user_page`, which the tool layer puts behind approval.

Swap this class for a DB-version adapter (Logseq HTTP API) without touching
the indexer or tools: they only use the public methods below."""

import hashlib
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import quote, unquote

LINK = re.compile(r"\[\[([^\[\]]+)\]\]")
TAG = re.compile(r"(?<![\w\[])#(?:\[\[([^\]]+)\]\]|([\w\-/.]+))")
PROP = re.compile(r"^\s*([A-Za-z0-9_\-]+)::\s*(.*)$")
TASK = re.compile(r"^(TODO|DOING|LATER|NOW|DONE|WAITING|CANCELED|CANCELLED)\s+(.*)", re.S)
STAMP = re.compile(r"(SCHEDULED|DEADLINE):\s*<(\d{4}-\d{2}-\d{2})[^>]*>")
JOURNAL_FILE = re.compile(r"^(\d{4})_(\d{2})_(\d{2})\.md$")
JARVIS_NS = "jarvis/"


@dataclass
class Block:
    page: str
    index: int
    text: str
    links: list[str] = field(default_factory=list)


@dataclass
class Task:
    page: str
    status: str
    text: str
    scheduled: str | None = None
    deadline: str | None = None


@dataclass
class Page:
    name: str
    path: Path
    is_journal: bool
    blocks: list[Block]
    properties: dict[str, str]

    @property
    def links(self) -> set[str]:
        out: set[str] = set()
        for b in self.blocks:
            out.update(b.links)
        for v in self.properties.values():
            out.update(_links(v))
        return out

    @property
    def digest(self) -> str:
        return hashlib.sha1("\n".join(b.text for b in self.blocks).encode()).hexdigest()


def _links(text: str) -> list[str]:
    found = [m.strip() for m in LINK.findall(text)]
    found += [(a or b).strip() for a, b in TAG.findall(text) if not a]  # #[[x]] already in LINK
    return list(dict.fromkeys(f.lower() for f in found if f))


def _file_to_name(stem: str) -> str:
    # :file/name-format :triple-lowbar  ->  "a___b" is page "a/b"; older graphs use %2F.
    return unquote(stem.replace("___", "/"))


def _name_to_file(name: str) -> str:
    safe = name.strip().replace("/", "___")
    safe = re.sub(r'[<>:"\\|?*\x00-\x1f]', "_", safe)
    if not safe or safe.startswith(".") or ".." in safe:
        raise ValueError(f"bad page name {name!r}")
    return safe + ".md"


def journal_title(d: date) -> str:
    return d.strftime("%b ") + f"{_ordinal(d.day)}, {d.year}"


def _ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def parse_blocks(page: str, text: str) -> tuple[list[Block], dict[str, str]]:
    blocks: list[Block] = []
    props: dict[str, str] = {}
    current: list[str] | None = None
    preamble = True
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.lstrip("\t ")
        if stripped.startswith("- ") or stripped == "-":
            preamble = False
            if current:
                blocks.append(_block(page, len(blocks), current))
            current = [stripped[2:]]
        elif preamble:
            if m := PROP.match(line):
                props[m.group(1).lower()] = m.group(2)
        elif current is not None and stripped:
            current.append(stripped)
    if current:
        blocks.append(_block(page, len(blocks), current))
    return [b for b in blocks if b.text], props


def _block(page: str, index: int, lines: list[str]) -> Block:
    text = "\n".join(lines).strip()
    return Block(page, index, text, _links(text))


class LogseqGraph:
    def __init__(self, root: str, name: str = "") -> None:
        self.root = Path(root)
        self._name = name
        self.pages_dir = self.root / "pages"
        self.journals_dir = self.root / "journals"
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self._name or self.root.resolve().name

    def deep_link(self, page: str) -> str:
        return f"logseq://graph/{quote(self.name)}?page={quote(page)}"

    # ---------- reading ----------
    def pages(self) -> list[Page]:
        out = []
        for folder, journal in ((self.pages_dir, False), (self.journals_dir, True)):
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.md")):
                out.append(self._load(path, journal))
        return out

    def page(self, name: str) -> Page | None:
        key = name.strip().lower()
        for p in self.pages():
            if p.name.lower() == key:
                return p
        return None

    def _load(self, path: Path, journal: bool) -> Page:
        name = _file_to_name(path.stem)
        if journal and (m := JOURNAL_FILE.match(path.name)):
            name = journal_title(date(int(m[1]), int(m[2]), int(m[3])))
        text = path.read_text(encoding="utf-8", errors="replace")
        blocks, props = parse_blocks(name, text)
        if title := props.get("title"):
            name = title.strip()
            for b in blocks:
                b.page = name
        return Page(name, path, journal, blocks, props)

    def tasks(self, include_done: bool = False) -> list[Task]:
        out = []
        for p in self.pages():
            for b in p.blocks:
                m = TASK.match(b.text)
                if not m or (not include_done and m[1] in ("DONE", "CANCELED", "CANCELLED")):
                    continue
                stamps = dict(STAMP.findall(b.text))
                first_line = STAMP.sub("", m[2]).strip().splitlines()[0] if m[2].strip() else ""
                out.append(Task(p.name, m[1], first_line, stamps.get("SCHEDULED"),
                                stamps.get("DEADLINE")))
        return out

    # ---------- writing ----------
    def append_journal(self, text: str, day: date | None = None) -> str:
        day = day or date.today()
        path = self.journals_dir / f"{day:%Y_%m_%d}.md"
        self._append(path, text)
        return journal_title(day)

    def write_jarvis_page(self, name: str, text: str, replace: bool = False) -> str:
        """Agent's own memory. Refuses anything outside the jarvis/ namespace."""
        if not name.lower().startswith(JARVIS_NS):
            name = JARVIS_NS + name
        return self._write_page(name, text, replace)

    def append_to_user_page(self, name: str, text: str) -> str:
        """Edits one of the user's pages. Callers must gate this behind approval."""
        return self._write_page(name, text, replace=False)

    # ---------- removing (callers gate these as destructive; everything goes to .trash/) ----------
    def _trash_dir(self) -> Path:
        trash = self.root / ".trash"
        trash.mkdir(exist_ok=True)
        return trash

    def _owned(self, name: str) -> Page:
        page = self.page(name.strip())
        if page is None:
            raise ValueError(f"no page called {name!r}")
        if page.path.parent.resolve() not in (self.pages_dir.resolve(), self.journals_dir.resolve()):
            raise ValueError("page is outside the graph")
        return page

    def trash_jarvis_page(self, name: str) -> str:
        """Move one of the agent's own jarvis/ pages to .trash/ (recoverable). User pages are refused."""
        if not name.strip().lower().startswith(JARVIS_NS):
            raise ValueError("only jarvis/ memory pages can be forgotten; use kb_delete_page for others")
        return self.trash_page(name)

    def trash_page(self, name: str) -> str:
        page = self._owned(name)
        with self._lock:
            page.path.replace(self._trash_dir() / f"{int(time.time())}_{page.path.name}")
        return page.name

    def trash_all_memory(self) -> list[str]:
        gone = [p for p in self.pages() if not p.is_journal and p.name.lower().startswith(JARVIS_NS)]
        with self._lock:
            for p in gone:
                p.path.replace(self._trash_dir() / f"{int(time.time())}_{p.path.name}")
        return [p.name for p in gone]

    def delete_blocks(self, name: str, match: str) -> int:
        """Remove every block (with its children) whose text contains `match`, ignoring case.
        The page as it was is kept in .trash/."""
        match = match.strip().lower()
        if len(match) < 3:
            raise ValueError("give at least 3 characters of the item to delete")
        page = self._owned(name)
        with self._lock:
            lines = page.path.read_text(encoding="utf-8").splitlines()
            out, removed, cut_below = [], 0, None
            for i, line in enumerate(lines):
                stripped = line.lstrip("\t ")
                indent = len(line) - len(stripped)
                if cut_below is not None:  # inside a removed block: drop its continuation and children
                    if not stripped or indent > cut_below or not stripped.startswith("- "):
                        continue
                    cut_below = None
                if stripped.startswith("- "):
                    body = [stripped[2:]]
                    for nxt in lines[i + 1:]:
                        ns = nxt.lstrip("\t ")
                        if not ns or ns.startswith("- "):
                            break
                        body.append(ns)
                    if match in " ".join(body).lower():
                        removed += 1
                        cut_below = indent
                        continue
                out.append(line)
            if removed:
                (self._trash_dir() / f"{int(time.time())}_{page.path.name}").write_text(
                    "\n".join(lines) + "\n", encoding="utf-8")
                page.path.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
        return removed

    def _write_page(self, name: str, text: str, replace: bool) -> str:
        path = (self.pages_dir / _name_to_file(name)).resolve()
        if path.parent != self.pages_dir.resolve():
            raise ValueError("page path escapes the graph")
        if replace:
            with self._lock:
                self.pages_dir.mkdir(parents=True, exist_ok=True)
                path.write_text(_as_blocks(text), encoding="utf-8")
        else:
            self._append(path, text)
        return name

    def _append(self, path: Path, text: str) -> None:
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            sep = "" if not existing or existing.endswith("\n") else "\n"
            path.write_text(existing + sep + _as_blocks(text), encoding="utf-8")


def _as_blocks(text: str) -> str:
    """Make sure free text lands as Logseq blocks ("- " bullets)."""
    lines = [ln for ln in text.strip().splitlines()]
    if any(ln.lstrip().startswith("- ") for ln in lines):  # already Logseq-formatted
        return "\n".join(lines) + "\n"
    return "- " + "\n  ".join(lines) + "\n"
