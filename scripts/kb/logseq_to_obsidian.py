"""Convert a Logseq file graph into the Obsidian vault layout the hub uses (kb/graph.py), in place.

    python -I scripts/kb/logseq_to_obsidian.py data/kb-graph            # dry run: say what would change
    python -I scripts/kb/logseq_to_obsidian.py data/kb-graph --apply    # do it (a full copy is kept first)

What changes:
  pages/a___b.md            -> a/b.md                (namespaces become folders; %2F names too)
  journals/2026_11_05.md    -> journals/2026-11-05.md
  page properties           `key:: value` lines before the first block -> YAML frontmatter
  [[Nov 5th, 2026]]         -> [[2026-11-05]]        (links to journal days)
  TODO/DOING/DONE ...       -> - [ ] / - [/] / - [x] / - [-]
  SCHEDULED: <d> / DEADLINE: <d>  -> ⏳ d / 📅 d    (Obsidian Tasks plugin)
  .trash/ copies            renamed to the new paths ("<seconds>_<path, / as ___>.md") and converted the same way
  logseq/                   removed (it is in the copy); .obsidian/ gets Daily notes = journals, YYYY-MM-DD
Block properties (learned::, via::) stay: Obsidian shows them as text and Dataview reads them as fields.
"""

import json
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

JOURNAL_OLD = re.compile(r"^(\d{4})_(\d{2})_(\d{2})$")
DAY_LINK = re.compile(r"\[\[((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w* \d{1,2}(?:st|nd|rd|th)?, \d{4})\]\]")
PROP = re.compile(r"^([A-Za-z0-9_\-]+)::\s*(.*)$")
TASK = re.compile(r"^(\s*- )(TODO|LATER|DOING|NOW|WAITING|DONE|CANCELED|CANCELLED)\s+(.*)$")
BOX = {"TODO": " ", "LATER": " ", "WAITING": " ", "DOING": "/", "NOW": "/", "DONE": "x", "CANCELED": "-",
       "CANCELLED": "-"}
STAMP = re.compile(r"\s*(SCHEDULED|DEADLINE):\s*<(\d{4}-\d{2}-\d{2})[^>]*>")


def day_name(title: str) -> str | None:
    t = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", title)
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def yaml_value(v: str) -> str:
    plain = re.fullmatch(r"[A-Za-z_][\w .\-/]*", v) and v.lower() not in ("true", "false", "null", "yes", "no")
    return v if plain else json.dumps(v, ensure_ascii=False)


def convert(text: str) -> str:
    lines = text.splitlines()
    props, i = [], 0
    while i < len(lines) and not lines[i].lstrip().startswith("- ") and lines[i].strip() != "-":
        if m := PROP.match(lines[i].strip()):
            props.append((m[1].lower(), m[2].strip()))
        elif lines[i].strip():
            break  # free text before the first block: leave the page head as it is
        i += 1
    else:
        pass
    body = lines[i:] if props else lines
    out = []
    for line in body:
        if m := TASK.match(line):
            rest, stamps = m[3], ""
            for kind, d in STAMP.findall(rest):
                stamps += f" {'⏳' if kind == 'SCHEDULED' else '📅'} {d}"
            line = f"{m[1]}[{BOX[m[2]]}] {STAMP.sub('', rest).rstrip()}{stamps}"
        elif STAMP.search(line) and line.strip().startswith(("SCHEDULED", "DEADLINE")):
            # a stamp on its own line under a task: move it onto the task line
            stamps = "".join(f" {'⏳' if k == 'SCHEDULED' else '📅'} {d}" for k, d in STAMP.findall(line))
            if out:
                out[-1] += stamps
                continue
        out.append(DAY_LINK.sub(lambda m: f"[[{day_name(m[1]) or m[1]}]]", line))
    props = [(k, v) for k, v in props if v]  # an empty property says nothing
    head = ("---\n" + "".join(f"{k}: {yaml_value(v)}\n" for k, v in props) + "---\n\n") if props else ""
    return head + "\n".join(out).strip("\n") + "\n"


def new_rel(old: Path, journal: bool) -> str:
    stem = old.stem
    if journal and (m := JOURNAL_OLD.match(stem)):
        return f"journals/{m[1]}-{m[2]}-{m[3]}.md"
    return "/".join(seg.strip() for seg in unquote(stem.replace("___", "/")).split("/")) + ".md"


def main(root: Path, apply: bool) -> None:
    moves: list[tuple[Path, Path, str]] = []  # (old, new, new text)
    for folder, journal in (("pages", False), ("journals", True)):
        for f in sorted((root / folder).glob("*.md")):
            moves.append((f, root / new_rel(f, journal), convert(f.read_text(encoding="utf-8"))))
    trash = []
    for f in sorted((root / ".trash").glob("*.md")) if (root / ".trash").is_dir() else []:
        m = re.match(r"^(\d+)_(.+)\.md$", f.name)
        if not m:
            continue
        stem = m[2]
        n = re.match(r"^(\d{4})_(\d{2})_(\d{2})((?:_\d+)?)$", stem)
        stem = f"journals___{n[1]}-{n[2]}-{n[3]}{n[4]}" if n else stem
        trash.append((f, f.with_name(f"{m[1]}_{stem}.md"), convert(f.read_text(encoding="utf-8", errors="replace"))))

    clashes = [new for _, new, _ in moves if new.exists() and new not in {o for o, _, _ in moves}]
    for old, new, _ in moves:
        print(f"{old.relative_to(root).as_posix():55} -> {new.relative_to(root).as_posix()}")
    print(f"{len(moves)} pages, {len(trash)} trash copies" + (f", CLASHES: {clashes}" if clashes else ""))
    if not apply or clashes:
        print("dry run: nothing changed (add --apply)" if not clashes else "not applied: files already exist")
        return

    backup = root.parent / f"{root.name}-logseq-backup-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copytree(root, backup)
    print("copy of the Logseq graph:", backup)
    for old, new, text in moves:
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_text(text, encoding="utf-8", newline="\n")
        if old.resolve() != new.resolve():
            old.unlink()
    for old, new, text in trash:
        new.write_text(text, encoding="utf-8", newline="\n")
        if old.resolve() != new.resolve():
            old.unlink()
    for gone in ("pages", "logseq"):
        p = root / gone
        if p.is_dir() and (gone == "logseq" or not any(p.iterdir())):
            shutil.rmtree(p)
    obs = root / ".obsidian"
    obs.mkdir(exist_ok=True)
    defaults = {"daily-notes.json": {"folder": "journals", "format": "YYYY-MM-DD"},
                "app.json": {"newLinkFormat": "absolute", "useMarkdownLinks": False, "trashOption": "local"}}
    for name, conf in defaults.items():
        f = obs / name
        have = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        f.write_text(json.dumps({**have, **conf}, indent=2), encoding="utf-8")
    print("done")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), "--apply" in sys.argv[2:])
