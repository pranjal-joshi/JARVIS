# 0002: The knowledge base is an Obsidian vault

Date: 2026-11-12 · Status: accepted · Supersedes [0001](0001-logseq-file-graph.md)

## Context

- 0001 chose Logseq's file-based graph so the hub could read and write plain markdown with no app running.
  The hub never used the Logseq app itself: `kb/graph.py` parses the files, Qdrant indexes them and the HUD
  draws its own graph. Logseq was only the editor, and it was not installed on the laptop.
- Logseq's development has moved to its DB version (still beta), so the file format 0001 relies on is no
  longer where Logseq is going. Obsidian is stable, keeps plain markdown files, has a strong mobile app, and
  since February 2026 an official headless sync client (`obsidian-headless`, open beta, needs Obsidian Sync).
- The graph was small (15 pages, 3 journal days), so converting it costs little.

## Decision

The KB folder is an **Obsidian vault**, and `kb/graph.py` (`ObsidianVault`) reads and writes its format:

| | Logseq (before) | Obsidian (now) |
|---|---|---|
| namespaced page | `pages/jarvis___people.md` | `jarvis/people.md` (folders) |
| journal day | `journals/2026_11_05.md`, `[[Nov 5th, 2026]]` | `journals/2026-11-05.md`, `[[2026-11-05]]` (Daily notes: folder `journals`, format `YYYY-MM-DD`) |
| page properties | `trigger:: webhook` lines before the first block | YAML frontmatter (`trigger: webhook`) |
| block properties | `learned:: …` under a block | unchanged: Dataview inline fields |
| tasks | `TODO` / `DONE`, `SCHEDULED: <…>` | `- [ ]` / `- [x]` (`[/]` doing, `[-]` cancelled), Tasks plugin `⏳ date` / `📅 date` |
| links from the HUD | `logseq://graph/…` | `obsidian://open?vault=…&file=…` |
| search index | Qdrant `logseq_blocks` | Qdrant `kb_blocks` (rebuilt from the files) |

Pages stay "- " bullet blocks, which Obsidian renders as lists, so memory, edits and moves keep working by
block. `[[name]]` also finds the one page in a folder with that file name, as Obsidian does
(`[[people]]` is `jarvis/people`). `.trash/` (hidden by Obsidian) keeps every copy JARVIS deletes or changes.

`scripts/kb/logseq_to_obsidian.py` converts a Logseq graph in place (dry run first; a full copy is kept).

## Consequences

- Edit the KB in Obsidian ("Open folder as vault" on `data/kb-graph`). The hub still needs no app running.
- Optional plugins make the fields richer, but nothing depends on them: Dataview (block fields as data),
  Tasks (the ⏳/📅 dates).
- Concurrent edits: as before, the hub only appends outside `jarvis/` and `skills/`; Obsidian reloads files
  that change on disk.
- Old Logseq-format pages that arrive later (a copied page) still read: `key::` lines before the first block
  count as page properties. Their `TODO` lines are not tasks until converted.
