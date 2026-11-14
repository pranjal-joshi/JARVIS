"""Obsidian vault adapter (markdown on disk). Reads everything; writes only to
the `jarvis/` namespace and the journal unless a caller explicitly uses
`append_to_user_page` (a `write` tool: the policy decides, and it asks after the turn read outside text).

Layout (the vault root is the graph root):
  <folder>/<page>.md      page "folder/page"; a namespace is a folder
  journals/YYYY-MM-DD.md  a daily note (Obsidian's Daily notes: folder "journals", format YYYY-MM-DD)
  .trash/                 copies of what was deleted or changed (Obsidian hides dot-folders)
Pages are "- " bullet blocks. Page properties are YAML frontmatter; a block's own properties are
`key:: value` lines under it (Dataview inline fields). Tasks are `- [ ]` checkboxes with the Tasks
plugin's dates (⏳ scheduled, 📅 due).

The indexer and tools only use the public methods below, so another store can replace this class."""

import difflib
import json
import hashlib
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import quote
from jarvis_hub.files import write_atomic

LINK = re.compile(r"\[\[([^\[\]|#]+)(?:#[^\[\]|]*)?(?:\|[^\[\]]*)?\]\]")  # [[page]], [[page#heading|alias]]
TAG = re.compile(r"(?<![\w\[])#(?:\[\[([^\]]+)\]\]|([\w\-/.]+))")
PROP = re.compile(r"^\s*([A-Za-z0-9_\-]+)::\s*(.*)$")
TASK = re.compile(r"^\[([ xX/\-])\]\s+(.*)", re.S)  # "- [ ] text": the block's text starts at "[ ]"
TASK_STATE = {" ": "TODO", "/": "DOING", "x": "DONE", "X": "DONE", "-": "CANCELLED"}
STAMP = re.compile(r"(⏳|📅)\uFE0F?\s*(\d{4}-\d{2}-\d{2})")  # Tasks plugin: ⏳ scheduled, 📅 due
STAMP_KIND = {"⏳": "SCHEDULED", "📅": "DEADLINE"}
FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)
JOURNAL_FILE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.md$")
JOURNALS = "journals"
HIDDEN = {JOURNALS, "logseq", "node_modules"}  # top-level folders that hold no pages (plus any dot-folder)
JARVIS_NS = "jarvis/"
ENTITIES_NS = "jarvis/entities/"  # one page per person, project, place...: aliases and relations in frontmatter
TRASH_FILE = re.compile(r"^(\d+)_(.+)\.md$")  # "<seconds>_<path, "/" as "___">[_n].md"
_MEMORY_TAG = re.compile(r"#jarvis\b|#\[\[jarvis\]\]|\[\[jarvis\]\]", re.I)  # notes the agent wrote


def _trashed(name: str) -> tuple[int, str] | None:
    """(when, the page's path in the vault) for a .trash/ copy "<seconds>_<path with "/" as "___">[_n].md".
    The "_n" that numbers two copies from the same second is dropped."""
    m = TRASH_FILE.match(name)
    if not m:
        return None
    stem = m[2]
    if n := re.match(r"^(.+)_\d+$", stem):
        stem = n[1]
    return int(m[1]), stem.replace("___", "/") + ".md"


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
    prose: list[Block] = field(default_factory=list)  # headings and paragraphs outside the "- " bullets

    @property
    def searchable(self) -> list[Block]:
        """What search indexes: the bullets, then the prose sections (a vault is mostly prose)."""
        return self.blocks + self.prose

    @property
    def links(self) -> set[str]:
        out: set[str] = set()
        for b in self.blocks + self.prose:
            out.update(b.links)
        for v in self.properties.values():
            out.update(_links(v))
        return out

    def listprop(self, key: str) -> list[str]:
        """A list property's items (`[a, b]`, a block list or "a, b"); a comma inside [[...]] stays."""
        return [x for x in (_unquote(x) for x in re.split(r",(?![^\[]*\]\])", self.properties.get(key, ""))) if x]

    @property
    def digest(self) -> str:
        # properties and prose count: a changed frontmatter or paragraph must re-index the page
        parts = [b.text for b in self.blocks] + ["\x00prose"] + [b.text for b in self.prose] + \
            ["\x00props"] + [f"{k}={v}" for k, v in sorted(self.properties.items())]
        return hashlib.sha1("\n".join(parts).encode()).hexdigest()


def _links(text: str) -> list[str]:
    found = [m.strip() for m in LINK.findall(text)]
    found += [(a or b).strip() for a, b in TAG.findall(text) if not a]  # #[[x]] already in LINK
    return list(dict.fromkeys(f.lower() for f in found if f))


def _rel_path(name: str) -> str:
    """Page name -> its file in the vault: "jarvis/people" -> "jarvis/people.md"."""
    parts = [re.sub(r'[<>:"\\|?*\x00-\x1f]', "_", seg.strip()) for seg in name.strip().split("/")]
    if not all(parts) or any(seg.startswith(".") for seg in parts) or parts[0].lower() in HIDDEN:
        raise ValueError(f"bad page name {name!r}")
    return "/".join(parts) + ".md"


def journal_title(d: date) -> str:
    """A day's journal page name, as Obsidian's daily notes name it."""
    return d.isoformat()


def _unquote(v: str) -> str:
    v = v.strip()
    return v[1:-1] if len(v) > 1 and v[0] == v[-1] and v[0] in "\"'" else v


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    """(page properties, the rest) from YAML frontmatter. Values stay strings: quotes dropped, a list's items
    joined by ", " (enough for what JARVIS reads: a skill's trigger, match and schedule)."""
    m = FRONTMATTER.match(text)
    if not m:
        return {}, text
    props: dict[str, str] = {}
    key = None
    for line in m[1].splitlines():
        if (item := re.match(r"^\s+-\s*(.*)$", line)) and key:
            props[key] = ", ".join(x for x in (props[key], _unquote(item[1])) if x)
        elif kv := re.match(r"^([A-Za-z0-9_\-]+):\s*(.*)$", line):
            key, v = kv[1].lower(), kv[2].strip()
            if v.startswith("[") and v.endswith("]"):
                v = ", ".join(_unquote(x) for x in v[1:-1].split(",") if x.strip())
            props[key] = _unquote(v)
    return props, text[m.end():]


def frontmatter(props: dict[str, str]) -> str:
    """YAML frontmatter for page properties; values YAML would read as something else are quoted."""
    def val(v: str) -> str:
        plain = re.fullmatch(r"[A-Za-z_][\w .\-/]*", v) and v.lower() not in ("true", "false", "null", "yes", "no")
        return v if plain else json.dumps(v, ensure_ascii=False)
    return "---\n" + "".join(f"{k}: {val(str(v))}\n" for k, v in props.items()) + "---\n"


PROSE_CHUNK = 900  # characters: a prose section longer than this is split at paragraphs
HEADING = re.compile(r"^#{1,6}\s+(.*)$")


def parse_blocks(page: str, text: str) -> tuple[list[Block], dict[str, str]]:
    blocks, props, _ = parse_page(page, text)
    return blocks, props


def parse_page(page: str, text: str) -> tuple[list[Block], dict[str, str], list[Block]]:
    """(bullet blocks, page properties, prose). Bullets are numbered as the edit tools count them; prose (the
    headings and paragraphs before the first bullet, or a page with no bullets at all) is kept apart, one block
    per heading section, so search finds it without changing what a block index points at."""
    blocks: list[Block] = []
    props, text = _frontmatter(text)
    current: list[str] | None = None
    preamble = True
    prose_lines: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.lstrip("\t ")
        if stripped.startswith("- ") or stripped == "-":
            preamble = False
            if current:
                blocks.append(_block(page, len(blocks), current))
            current = [stripped[2:]]
        elif preamble:
            if m := PROP.match(line):  # "key:: value" before the first block: a page property as well
                props.setdefault(m.group(1).lower(), m.group(2))
            else:
                prose_lines.append(line)
        elif current is not None and stripped:
            current.append(stripped)
    if current:
        blocks.append(_block(page, len(blocks), current))
    return [b for b in blocks if b.text], props, _prose(page, prose_lines)


def _prose(page: str, lines: list[str]) -> list[Block]:
    """Heading sections of free text, each starting with its heading so a hit says what it is about."""
    sections: list[list[str]] = [[]]
    for line in lines:
        if HEADING.match(line.strip()) and any(x.strip() for x in sections[-1]):
            sections.append([])
        sections[-1].append(line)
    out: list[Block] = []
    for sec in sections:
        heading = ""
        paras, para = [], []
        for line in sec:
            st = line.strip()
            if h := HEADING.match(st):
                heading = h[1].strip()
            elif st:
                para.append(st)
            elif para:
                paras.append(" ".join(para))
                para = []
        if para:
            paras.append(" ".join(para))
        chunk = ""
        for p in paras:
            if chunk and len(chunk) + len(p) > PROSE_CHUNK:
                out.append(_prose_block(page, len(out), heading, chunk))
                chunk = ""
            chunk = f"{chunk}\n{p}" if chunk else p
        if chunk:
            out.append(_prose_block(page, len(out), heading, chunk))
    return [b for b in out if b.text]


def _prose_block(page: str, n: int, heading: str, body: str) -> Block:
    text = f"{heading}\n{body}".strip() if heading else body.strip()
    return Block(page, -1 - n, text, _links(text))  # negative: not a bullet the edit tools can point at


def _block(page: str, index: int, lines: list[str]) -> Block:
    text = "\n".join(lines).strip()
    return Block(page, index, text, _links(text))


class ObsidianVault:
    def __init__(self, root: str, name: str = "") -> None:
        self.root = Path(root)
        self._name = name
        self.pages_dir = self.root
        self.journals_dir = self.root / JOURNALS
        self._lock = threading.Lock()
        # parsed pages by file, kept while the file's (mtime, size) is unchanged: pages() runs on every
        # lookup, fast-path match and schedule tick, and re-parsing the whole vault each time does not scale
        self._cache: dict[Path, tuple[tuple[int, int], Page]] = {}

    @property
    def name(self) -> str:
        return self._name or self.root.resolve().name

    def deep_link(self, page: str) -> str:
        """obsidian:// link that opens the page in the vault (the vault's name in Obsidian: KB_GRAPH_NAME)."""
        p = self.page(page)
        file = p.path.relative_to(self.root).as_posix()[:-3] if p else page
        return f"obsidian://open?vault={quote(self.name)}&file={quote(file)}"

    # ---------- reading ----------
    def _page_files(self) -> list[tuple[Path, bool]]:
        if not self.root.is_dir():
            return []
        out = []
        for path in sorted(self.root.rglob("*.md")):
            rel = path.relative_to(self.root).parts
            if any(part.startswith(".") for part in rel):
                continue
            if rel[0] == JOURNALS:
                if len(rel) == 2 and JOURNAL_FILE.match(rel[1]):
                    out.append((path, True))
            elif rel[0].lower() not in HIDDEN:
                out.append((path, False))
        return out

    def pages(self) -> list[Page]:
        files = self._page_files()
        out = []
        for path, journal in files:
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            stamp = (st.st_mtime_ns, st.st_size)
            hit = self._cache.get(path)
            if hit is None or hit[0] != stamp:
                hit = (stamp, self._load(path, journal))
                self._cache[path] = hit
            out.append(hit[1])
        if len(self._cache) > len(files):
            keep = {p for p, _ in files}
            for p in [p for p in self._cache if p not in keep]:
                self._cache.pop(p, None)
        return out

    def page(self, name: str, exact: bool = False) -> Page | None:
        """The page called `name`, or the one page whose file name is `name` (Obsidian links a page in a folder
        by its file name alone when that is unique: [[people]] is jarvis/people)."""
        key = name.strip().lower()
        pages = self.pages()
        for p in pages:
            if p.name.lower() == key:
                return p
        if key and "/" not in key and not exact:
            short = [p for p in pages if not p.is_journal and p.name.lower().rsplit("/", 1)[-1] == key]
            if len(short) == 1:
                return short[0]
        if key and not exact and (named := self.entity_aliases().get(key)):  # [[Z2M]] -> jarvis/entities/zigbee2mqtt
            return next((p for p in pages if p.name.lower() == named), None)
        return None

    def entity_aliases(self) -> dict[str, str]:
        """Each entity page's alias (lowercase) -> that page's name (lowercase)."""
        out: dict[str, str] = {}
        for p in self.pages():
            if p.name.lower().startswith(ENTITIES_NS):
                for a in p.listprop("aliases"):
                    out.setdefault(a.lower(), p.name.lower())
        return out

    @staticmethod
    def resolve(link: str, names: set[str], aliases: dict[str, str] | None = None) -> str:
        """A [[link]] (lowercase) as the page it points at: itself, the one page in a folder whose file name
        it is, or the entity page it is an alias of. names: every page name, lowercase; aliases: entity_aliases()."""
        if link in names or "/" in link:
            return link
        short = [n for n in names if n.rsplit("/", 1)[-1] == link]
        return short[0] if len(short) == 1 else (aliases or {}).get(link, link)

    def similar_pages(self, name: str, n: int = 3) -> list[str]:
        """Page names close to `name` (typos, singular/plural, a missing namespace)."""
        key = name.strip().lower()
        names = {p.name.lower(): p.name for p in self.pages()}
        hits = difflib.get_close_matches(key, list(names), n=n, cutoff=0.6)
        hits += [k for k in names if key and (key in k or k.rsplit("/", 1)[-1] == key) and k not in hits]
        return [names[k] for k in hits[:n]]

    def _load(self, path: Path, journal: bool) -> Page:
        name = path.stem if journal else path.relative_to(self.root).as_posix()[:-3]
        text = path.read_text(encoding="utf-8", errors="replace")
        blocks, props, prose = parse_page(name, text)
        return Page(name, path, journal, blocks, props, prose)

    def tasks(self, include_done: bool = False) -> list[Task]:
        out = []
        for p in self.pages():
            for b in p.blocks:
                m = TASK.match(b.text)
                if not m:
                    continue
                status = TASK_STATE[m[1]]
                if not include_done and status in ("DONE", "CANCELLED"):
                    continue
                stamps = {STAMP_KIND[k]: d for k, d in STAMP.findall(b.text)}
                first_line = STAMP.sub("", m[2]).strip().splitlines()[0].strip() if m[2].strip() else ""
                out.append(Task(p.name, status, first_line, stamps.get("SCHEDULED"), stamps.get("DEADLINE")))
        return out

    # ---------- writing ----------
    def append_journal(self, text: str, day: date | None = None) -> str:
        day = day or date.today()
        path = self.journals_dir / f"{day:%Y-%m-%d}.md"
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

    def _trash_path(self, file: Path | str) -> Path:
        """Where a page goes in .trash/: "<seconds>_<its path, "/" as "___">", numbered when two land in the
        same second (deleting two points quickly must keep both copies). file: the page's file, or its path in
        the vault."""
        if isinstance(file, Path):
            file = file.resolve().relative_to(self.root.resolve()).as_posix()
        stem, n = f"{int(time.time())}_{file.replace('/', '___')}", 1
        path = self._trash_dir() / stem
        while path.exists():
            n += 1
            path = self._trash_dir() / f"{stem.rsplit('.', 1)[0]}_{n}.{stem.rsplit('.', 1)[-1]}"
        return path

    def _owned(self, name: str) -> Page:
        page = self.page(name.strip())
        if page is None:
            raise ValueError(f"no page called {name!r}")
        rel = page.path.resolve().relative_to(self.root.resolve()).parts
        if any(part.startswith(".") for part in rel):
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
            page.path.replace(self._trash_path(page.path))
            self._prune(page.path.parent)
        return page.name

    def trash_all_memory(self) -> tuple[list[str], int]:
        """All of the agent's memory: its jarvis/ pages (moved to .trash/) and the #jarvis notes it wrote
        in journals (removed; the journal as it was is kept in .trash/). User-written lines stay."""
        gone = [p for p in self.pages() if not p.is_journal and p.name.lower().startswith(JARVIS_NS)]
        with self._lock:
            for p in gone:
                p.path.replace(self._trash_path(p.path))
                self._prune(p.path.parent)
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
        self._trash_path(page.path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._save(page.path, "\n".join(out) + ("\n" if out else ""))

    def delete_block(self, name: str, index: int, text: str) -> None:
        """Remove the block at `index` with what is nested under it, if its text is still `text`. The page as
        it was is kept in .trash/."""
        self.take_block(name, index, text)

    def take_block(self, name: str, index: int, text: str) -> str:
        """delete_block, returning the block (with what is nested under it) as markdown for another page."""
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
        page = self.page(name.strip(), exact=True)
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
        if self.page(to, exact=True) is not None:
            raise ValueError(f"there's already a page called {to!r}")
        target = self._path(to)
        names = [re.escape(page.name)]
        if "/" in page.name and len(self.similar_short(page.name)) == 1:  # [[people]] for jarvis/people
            names.append(re.escape(page.name.rsplit("/", 1)[-1]))
        alt = "|".join(names)
        refs = re.compile(rf"(#?)\[\[(?:{alt})((?:#[^\[\]|]*)?(?:\|[^\[\]]*)?)\]\]"
                          rf"|(?<![\w\[])#(?:{alt})(?![\w\-/.])", re.I)
        tag = f"#{to}" if re.fullmatch(r"[\w\-/.]+", to) else f"#[[{to}]]"
        changed = 0
        with self._lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            page.path.replace(target)
            self._prune(page.path.parent)
            for p in self.pages():
                text = p.path.read_text(encoding="utf-8")
                out = refs.sub(lambda m: f"{m[1]}[[{to}{m[2] or ''}]]" if m[0].lstrip("#").startswith("[[")
                               else tag, text)
                if out != text:
                    self._trash_path(p.path).write_text(text, encoding="utf-8")
                    self._save(p.path, out)
                    changed += 1
        return to, changed

    # ---------- .trash/ ----------
    def trash(self) -> list[dict]:
        """What .trash/ holds, newest first: each copy's page, when it went, and whether that page exists now
        (a copy of a page that still exists is how it was before a point was deleted or changed)."""
        folder = self.root / ".trash"
        if not folder.is_dir():
            return []
        here = {p.path.relative_to(self.root).as_posix().lower() for p in self.pages()}
        out = []
        for f in folder.glob("*.md"):
            if not (t := _trashed(f.name)):
                continue
            at, file = t
            j = _journal_file(file)
            name = file.split("/", 1)[1][:-3] if j else file[:-3]
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
        journal = _journal_file(file)
        target = (self.root / file).resolve()
        if self.root.resolve() not in target.parents or any(x.startswith(".") for x in file.split("/")):
            raise KeyError("no such copy in the trash")
        with self._lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.replace(self._trash_path(file))
            copy.replace(target)
        return self._load(target, journal).name

    def take_blocks(self, name: str, match: str) -> list[str]:
        """Remove every block containing `match` (with its children) and return each as it was written, as
        markdown to put on another page. The page as it was is kept in .trash/."""
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
                self._trash_path(page.path).write_text(
                    "\n".join(lines) + "\n", encoding="utf-8")
                self._save(page.path, "\n".join(out) + ("\n" if out else ""))
        return removed

    def _path(self, name: str) -> Path:
        """Where page `name` is (or would be) in the vault."""
        path = (self.root / _rel_path(name)).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError("page path escapes the graph")
        return path

    def similar_short(self, name: str) -> list[str]:
        """Pages with the same file name as `name` (a bare [[file name]] link is a page's only when unique)."""
        short = name.lower().rsplit("/", 1)[-1]
        return [p.name for p in self.pages() if not p.is_journal and p.name.lower().rsplit("/", 1)[-1] == short]

    def _prune(self, folder: Path) -> None:
        """Remove folders a page move or delete left empty, up to the vault root."""
        root, folder = self.root.resolve(), folder.resolve()
        while folder != root and root in folder.parents and folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
            folder = folder.parent

    def _write_page(self, name: str, text: str, replace: bool) -> str:
        path = self._path(name)
        if replace:
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._save(path, _as_blocks(text))
        else:
            self._append(path, text)
        return name

    def _append(self, path: Path, text: str) -> None:
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            sep = "" if not existing or existing.endswith("\n") else "\n"
            self._save(path, existing + sep + _as_blocks(text))

    def _save(self, path: Path, text: str) -> None:
        """Write a page atomically (temp file, then replace): Obsidian or Sync reading it mid-write, or a crash,
        never sees half a file. Callers hold the lock."""
        write_atomic(path, text)
        self._cache.pop(path, None)


def _as_blocks(text: str) -> str:
    """Make sure free text lands as blocks ("- " bullets); a page that starts with frontmatter stays as it is."""
    lines = [ln for ln in text.strip().splitlines()]
    if lines[:1] == ["---"] or any(ln.lstrip().startswith("- ") for ln in lines):  # already block-formatted
        return "\n".join(lines) + "\n"
    return "- " + "\n  ".join(lines) + "\n"


def _journal_file(rel: str) -> bool:
    """Is this vault path a daily note ("journals/2026-11-05.md")?"""
    folder, _, file = rel.partition("/")
    return folder == JOURNALS and bool(JOURNAL_FILE.match(file))
