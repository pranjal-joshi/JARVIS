"""Logseq file-based graph adapter (markdown on disk). Reads everything; writes
only to the `jarvis/` namespace and the journal unless a caller explicitly
uses `append_to_user_page`, which the tool layer puts behind approval.

Swap this class for a DB-version adapter (Logseq HTTP API) without touching
the indexer or tools: they only use the public methods below."""

import difflib
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
TRASH_FILE = re.compile(r"^(\d+)_(.+)\.md$")  # "<seconds>_<file stem>[_n].md"
_MEMORY_TAG = re.compile(r"#jarvis\b|#\[\[jarvis\]\]|\[\[jarvis\]\]", re.I)  # notes the agent wrote


def _trashed(name: str) -> tuple[int, str] | None:
    """(when, the page's file) for a .trash/ copy "<seconds>_<file stem>[_n].md". The "_n" that numbers two copies
    from the same second is dropped, but a journal day's own "_03" is not."""
    m = TRASH_FILE.match(name)
    if not m:
        return None
    stem = m[2]
    if not JOURNAL_FILE.match(stem + ".md") and (n := re.match(r"^(.+)_\d+$", stem)):
        stem = n[1]
    return int(m[1]), stem + ".md"


def _is_memory_note(text: str) -> bool:
    return bool(_MEMORY_TAG.search(text))


def _check_match(match: str) -> str:
    m = match.strip().lower()
    if len(m) < 3:
        raise ValueError("give at least 3 characters of the item to delete")
    return m


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

    def similar_pages(self, name: str, n: int = 3) -> list[str]:
        """Page names close to `name` (typos, singular/plural, a missing namespace)."""
        key = name.strip().lower()
        names = {p.name.lower(): p.name for p in self.pages()}
        hits = difflib.get_close_matches(key, list(names), n=n, cutoff=0.6)
        hits += [k for k in names if key and (key in k or k.rsplit("/", 1)[-1] == key) and k not in hits]
        return [names[k] for k in hits[:n]]

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

    def replace_page(self, name: str, text: str) -> str:
        """Write a whole page (a skill page made or changed by its tools). Callers keep this to their namespace."""
        return self._write_page(name, text, replace=True)

    def append_to_user_page(self, name: str, text: str) -> str:
        """Edits one of the user's pages. Callers must gate this behind approval."""
        return self._write_page(name, text, replace=False)

    # ---------- removing (callers gate these as destructive; everything goes to .trash/) ----------
    def _trash_dir(self) -> Path:
        trash = self.root / ".trash"
        trash.mkdir(exist_ok=True)
        return trash

    def _trash_path(self, file: str) -> Path:
        """Where a page goes in .trash/: "<seconds>_<file>", numbered when two land in the same second
        (deleting two points quickly must keep both copies)."""
        stem, n = f"{int(time.time())}_{file}", 1
        path = self._trash_dir() / stem
        while path.exists():
            n += 1
            path = self._trash_dir() / f"{stem.rsplit('.', 1)[0]}_{n}.{stem.rsplit('.', 1)[-1]}"
        return path

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
            page.path.replace(self._trash_path(page.path.name))
        return page.name

    def trash_all_memory(self) -> tuple[list[str], int]:
        """All of the agent's memory: its jarvis/ pages (moved to .trash/) and the #jarvis notes it wrote
        in journals (removed; the journal as it was is kept in .trash/). User-written lines stay."""
        gone = [p for p in self.pages() if not p.is_journal and p.name.lower().startswith(JARVIS_NS)]
        with self._lock:
            for p in gone:
                p.path.replace(self._trash_path(p.path.name))
        notes = sum(self._drop_blocks(p, _is_memory_note) for p in self.pages() if p.is_journal)
        return [p.name for p in gone], notes

    def memory_items(self, match: str) -> list[tuple[str, str]]:
        """(page, text) of the remembered facts on jarvis/ pages that contain `match`, ignoring case."""
        m = _check_match(match)
        return [(p.name, b.text) for p in self.pages() if not p.is_journal and p.name.lower().startswith(JARVIS_NS)
                for b in p.blocks if m in b.text.lower()]

    def delete_memory_items(self, match: str) -> list[tuple[str, int]]:
        """Remove memory items containing `match` from jarvis/ pages and #jarvis journal notes.
        Returns (page, count) for each page that changed."""
        m = _check_match(match)
        out = []
        for p in self.pages():
            if p.is_journal:
                n = self._drop_blocks(p, lambda t: m in t.lower() and _is_memory_note(t))
            elif p.name.lower().startswith(JARVIS_NS):
                n = self._drop_blocks(p, lambda t: m in t.lower())
            else:
                continue
            if n:
                out.append((p.name, n))
        return out

    def delete_blocks(self, name: str, match: str) -> int:
        """Remove every block (with its children) whose text contains `match`, ignoring case.
        The page as it was is kept in .trash/."""
        m = _check_match(match)
        return self._drop_blocks(self._owned(name), lambda t: m in t.lower())

    @staticmethod
    def _locate(lines: list[str], index: int, text: str) -> tuple[int, int, int, int]:
        """The block at `index` (as parse_blocks numbers them): (first line, end of its own lines, end with what
        is nested under it, indent). Raises if its text is no longer `text`: a page edited since it was read
        must not lose or change the wrong line."""
        starts = [i for i, line in enumerate(lines) if line.lstrip("\t ").startswith("- ")
                  or line.lstrip("\t ") == "-"]
        if not 0 <= index < len(starts):
            raise ValueError("that point is no longer on the page; reload it")
        first = starts[index]
        indent = len(lines[first]) - len(lines[first].lstrip("\t "))
        end = first + 1
        while end < len(lines) and not (lines[end].lstrip("\t ").startswith("- ")
                                        and len(lines[end]) - len(lines[end].lstrip("\t ")) <= indent):
            end += 1
        own, own_end = [lines[first].lstrip("\t ")[2:]], first + 1  # the block's own lines, as parse_blocks reads them
        for line in lines[first + 1:end]:
            if line.lstrip("\t ").startswith("- "):
                break
            own_end += 1
            if line.strip():
                own.append(line.strip())
        if "\n".join(own).strip() != text.strip():
            raise ValueError("the page changed since it was read; reload it")
        return first, own_end, end, indent

    def _rewrite(self, page: Page, lines: list[str], out: list[str]) -> None:
        """Write `out` to the page; the page as it was (`lines`) is kept in .trash/."""
        self._trash_path(page.path.name).write_text("\n".join(lines) + "\n", encoding="utf-8")
        page.path.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")

    def delete_block(self, name: str, index: int, text: str) -> None:
        """Remove the block at `index` with what is nested under it, if its text is still `text`. The page as
        it was is kept in .trash/."""
        self.take_block(name, index, text)

    def take_block(self, name: str, index: int, text: str) -> str:
        """delete_block, returning the block (with what is nested under it) as Logseq text for another page."""
        page = self._owned(name)
        with self._lock:
            lines = page.path.read_text(encoding="utf-8").splitlines()
            first, _, end, indent = self._locate(lines, index, text)
            self._rewrite(page, lines, lines[:first] + lines[end:])
        return "\n".join(line[indent:] if line[:indent].strip() == "" else line.lstrip() for line in lines[first:end])

    def edit_block(self, name: str, index: int, text: str, new: str) -> None:
        """Replace what the block at `index` says, if it still says `text`. Its properties (learned::, via::) and
        what is nested under it stay; a remembered fact's via:: becomes "edited by you"."""
        said = [ln.strip() for ln in new.strip().splitlines() if ln.strip() and not PROP.match(ln)]
        if not said:
            raise ValueError("a point can't be empty; delete it instead")
        page = self._owned(name)
        with self._lock:
            lines = page.path.read_text(encoding="utf-8").splitlines()
            first, own_end, _, indent = self._locate(lines, index, text)
            pad = lines[first][:indent]
            props = [ln for ln in lines[first + 1:own_end] if PROP.match(ln)]
            props = [re.sub(r"(via::\s*).*", r"\1edited by you", ln) for ln in props]
            body = [f"{pad}- {said[0]}"] + [f"{pad}  {ln}" for ln in said[1:]]
            self._rewrite(page, lines, lines[:first] + body + props + lines[own_end:])

    def add_block(self, name: str, text: str) -> str:
        """A new point at the end of a page (an existing journal day included); a new page is made if needed."""
        page = self.page(name.strip())
        if page is None:
            return self._write_page(name.strip(), text, replace=False)
        self._append(self._owned(page.name).path, text)
        return page.name

    def rename_page(self, name: str, to: str) -> tuple[str, int]:
        """Rename a page (journal days keep their date) and point every [[link]] and #tag at the new name.
        Returns (new name, pages whose links changed); each changed page is kept in .trash/ as it was."""
        page, to = self._owned(name), to.strip()
        if page.is_journal:
            raise ValueError("journal days are named by their date")
        if not to or to.lower() == page.name.lower():
            raise ValueError("give the page a different name")
        if self.page(to) is not None:
            raise ValueError(f"there's already a page called {to!r}")
        target = (self.pages_dir / _name_to_file(to)).resolve()
        if target.parent != self.pages_dir.resolve():
            raise ValueError("page path escapes the graph")
        old = re.escape(page.name)
        refs = re.compile(rf"#?\[\[{old}\]\]|(?<![\w\[])#{old}(?![\w\-/.])", re.I)
        changed = 0
        with self._lock:
            page.path.replace(target)
            for p in self.pages():
                text = p.path.read_text(encoding="utf-8")
                out = refs.sub(lambda m: ("#" if m[0].startswith("#") else "") + f"[[{to}]]", text)
                if out != text:
                    self._trash_path(p.path.name).write_text(text, encoding="utf-8")
                    p.path.write_text(out, encoding="utf-8")
                    changed += 1
        return to, changed

    # ---------- .trash/ ----------
    def trash(self) -> list[dict]:
        """What .trash/ holds, newest first: each copy's page, when it went, and whether that page exists now
        (a copy of a page that still exists is how it was before a point was deleted or changed)."""
        folder = self.root / ".trash"
        if not folder.is_dir():
            return []
        here = {p.path.name.lower() for p in self.pages()}
        out = []
        for f in folder.glob("*.md"):
            if not (t := _trashed(f.name)):
                continue
            at, file = t
            j = JOURNAL_FILE.match(file)
            name = journal_title(date(int(j[1]), int(j[2]), int(j[3]))) if j else _file_to_name(file[:-3])
            blocks, _ = parse_blocks(name, f.read_text(encoding="utf-8", errors="replace"))
            out.append({"id": f.name, "page": name, "at": at, "points": len(blocks),
                        "journal": bool(j), "exists": file.lower() in here})
        return sorted(out, key=lambda t: (-t["at"], t["id"]))

    def restore(self, trash_id: str) -> str:
        """Put a copy from .trash/ back. The page as it is now (if it exists) goes to .trash/ first."""
        t = _trashed(trash_id)
        copy = self.root / ".trash" / trash_id
        if not t or "/" in trash_id or "\\" in trash_id or trash_id.startswith(".") or not copy.is_file():
            raise KeyError("no such copy in the trash")
        file = t[1]
        journal = bool(JOURNAL_FILE.match(file))
        folder = self.journals_dir if journal else self.pages_dir
        target = folder / file
        with self._lock:
            folder.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.replace(self._trash_path(file))
            copy.replace(target)
        return self._load(target, journal).name

    def take_blocks(self, name: str, match: str) -> list[str]:
        """Remove every block containing `match` (with its children) and return each as it was written, as
        Logseq text to put on another page. The page as it was is kept in .trash/."""
        m, taken = _check_match(match), []
        self._drop_blocks(self._owned(name), lambda t: m in t.lower(), taken)
        out = []
        for group in taken:
            cut = len(group[0]) - len(group[0].lstrip("\t "))
            out.append("\n".join(line[cut:] if line[:cut].strip() == "" else line.lstrip() for line in group))
        return out

    def _drop_blocks(self, page: Page, drop, taken: list | None = None) -> int:
        """Remove every top-or-nested block (with its children) for which drop(text) is true. taken: each
        removed block's lines are added to it, one list per block."""
        with self._lock:
            lines = page.path.read_text(encoding="utf-8").splitlines()
            out, removed, cut_below = [], 0, None
            for i, line in enumerate(lines):
                stripped = line.lstrip("\t ")
                indent = len(line) - len(stripped)
                if cut_below is not None:  # inside a removed block: drop its continuation and children
                    if not stripped or indent > cut_below or not stripped.startswith("- "):
                        if taken is not None and stripped:
                            taken[-1].append(line)
                        continue
                    cut_below = None
                if stripped.startswith("- "):
                    body = [stripped[2:]]
                    for nxt in lines[i + 1:]:
                        ns = nxt.lstrip("\t ")
                        if not ns or ns.startswith("- "):
                            break
                        body.append(ns)
                    if drop(" ".join(body)):
                        removed += 1
                        cut_below = indent
                        if taken is not None:
                            taken.append([line])
                        continue
                out.append(line)
            if removed:
                self._trash_path(page.path.name).write_text(
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
