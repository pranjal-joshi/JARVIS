# 0001: Logseq integration starts with the file-based graph

Date: 2026-11-01 · Status: superseded by [0002](0002-obsidian-vault.md) on 2026-11-12

## Context

- Logseq's newest line (2.0.x, latest 2.0.2 on 2026-10-07) is the **DB version**, still labelled Beta.
  Its graph lives in a database inside the app; integration goes through the app's HTTP API, which
  needs the desktop app running.
- The file-based version stores plain markdown (`pages/`, `journals/`) that any process can read,
  sync (Syncthing, git) and host headless on a homelab.
- There was no existing graph, so nothing has to be migrated.

## Decision

The hub reads and writes the graph as **markdown files** (`hub/src/jarvis_hub/kb/graph.py`), mounted
into the hub container. Qdrant is only a search index and can be rebuilt from the files at any time.
The adapter has a small interface (pages, page, tasks, append_journal, write_jarvis_page,
append_to_user_page) so a DB-version adapter over the Logseq HTTP API can replace it later.

## Consequences

- Works headless on Proxmox with no Logseq app running; you edit with the Logseq desktop/mobile app
  pointed at the same folder (synced).
- Concurrent edits: the hub only *appends* outside `jarvis/`, and Logseq re-reads files on change.
  Simultaneous edits to the same journal file can still conflict. Acceptable for the POC.
- Revisit when the DB version leaves Beta and has a documented headless/server mode.
