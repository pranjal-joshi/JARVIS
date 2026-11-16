# Status: 2026-11-01 (overnight build)

The prototype runs on this laptop: `docker compose --profile voice up -d` → http://localhost:3000.

## Done and verified

Verified on this machine (CPU only, qwen3.5:4b), not just written:

| Area | What works | How it was checked |
|---|---|---|
| Stack | hub, qdrant, ollama, ui, whisper, piper all up; hub healthy; all ports loopback-only | `docker compose ps`, curl |
| Auth / routing | per-client tokens decide the source; events reach only that source; one-time WS tickets keep the token out of the browser | 19 hub tests |
| Agent | real tool calling with qwen3.5:4b (thinking off), persona, KB lookups | chat via API and HUD |
| Approval gate | what policy.yaml holds (destructive, external; skills; writes after outside text) waits for HUD Approve/Deny; denied/timed-out calls never run; satellite token gets 403 on approvals | API run (deny → nothing written, approve → line written) and HUD click-through |
| Audit | every call logged with source, risk, status, approver | `/v1/audit`, HUD timeline |
| Obsidian KB | vault read (folders = namespaces, daily notes in journals/), `- [ ]` tasks with ⏳/📅 dates, `jarvis/` memory + journal writes, user-page edits gated | tests + live |
| Qdrant | nomic-embed-text embeddings, semantic search ("evening drink" → tea preference) | `/v1/kb/search` |
| Prompt injection | "Untrusted Clipping" page: JARVIS reports the injected command and does not act | HUD |
| Voice (hub path) | Piper TTS (4 s) → Whisper STT (3 s) round trip, exact transcript | curl |
| HUD | orb, chat with typewriter, timeline (calls/results/recalls/audit), approval cards, memory panel with `obsidian://` links, voice toggle, mic button, mobile layout, reduced motion | built-in browser |
| HA side | **not connected**, as you asked: no HA files or containers touched. Guide ready: [ha-setup.md](ha-setup.md) | `compose.ha.yaml` validates |

## Needs you (in this order)

1. **Home Assistant link** ([ha-setup.md](ha-setup.md), about 15 min): non-admin JARVIS user + token → `HA_TOKEN` in `.env`,
   add *Model Context Protocol Server*, expose a test light, start with `compose.ha.yaml`.
   Then *"Turn on the desk lamp"* in the HUD → Approve.
2. **HA Assist → JARVIS**: point *Local OpenAI LLM* at `http://hub:8700/v1` with the satellite token;
   add Wyoming whisper/piper; build the `JARVIS` assist pipeline (same guide, §3–5).
3. **Cloned voice**: your clip is converted (`voice/reference/jarvis.wav`, 14.8 s), VoiceStudio is running and
   the `jarvis` profile exists. Left for you, at http://localhost:3900: accept the OmniVoice model licence
   (CC-BY-NC, non-commercial) in Model Catalogue so the ~2.4 GB weights download, and make the profile's
   consent/attestation choice if VoiceStudio asks. Until then the hub speaks with Piper ([voice/README.md](../voice/README.md)).
4. **Mic in the HUD**: not tested overnight (no microphone in the automated browser). Try ◉ → speak → ■.
5. **Your Obsidian vault**: open `data/kb-graph` as a vault in Obsidian (*Open folder as vault*); set
   `KB_GRAPH_NAME` to the vault name Obsidian shows so the HUD's *open in Obsidian* links work. A Logseq
   graph converts with `scripts/kb/logseq_to_obsidian.py` ([decision 0002](decisions/0002-obsidian-vault.md)).

## Known issues / limits

- **Speed**: now 2–4 s per reply with `jarvis-2b-gpu` fully on the MX550 (was 11–57 s with qwen3.5:4b on CPU).
  STT/TTS/embeddings stay on CPU because the LLM fills the 2 GB card.
- **Small-model judgement**: qwen3.5:2b is weaker than 4b. Seen once in testing: it wrote a fake
  `<tool_result>` claiming success instead of calling the tool. The hub now detects that, asks the model
  again once, and otherwise says nothing was changed. Expect occasional odd phrasing ("I drink tea…").
- **Approvals from voice** no longer block the HA request: the satellite says "I've asked on your phone and
  the HUD" at once, and announces the result when you decide (`VOICE_ASYNC_APPROVAL_S`, default 10 min). The
  rest of a request with several parts doesn't run after such a pause.
- **Hugging Face downloads were flaky** on this network: the Piper voice first arrived truncated
  (16 of 63 MB) and had to be re-fetched. If a Wyoming container logs `INVALID_PROTOBUF`,
  delete the file in its volume and restart it.
- Image tags are pinned, but digests aren't locked yet: run `scripts/wsl/pin-digests.sh`.
- Your existing ha-docker stack uses unpinned `:rc` / `:latest` tags (outside this repo; just flagging).

## Next phases

| Phase | Scope |
|---|---|
| 6 | Filesystem MCP (read-only, scoped folders), Desktop Commander (destructive-gated), SSH/Docker MCP for homelab (read-only first); approvals persisted in SQLite |
| 7 | Music Assistant + Sendspin BT bridge (usbipd dongle; check WSL BT reliability), ESP32-S3 satellite YAML with `hey_jarvis`, `on_tts_end` → `music_assistant.play_announcement` |
| 8 | 3D knowledge graph panel (3d-force-graph) from `/v1/kb/graph`, lighting up `kb_retrieved` pages |
| 9 | Phone tools (HA Companion notifications, later ADB MCP); R3F/GLSL orb, bloom, tool-call beams, passkey auth, Tailscale |
| 10 | Proxmox migration guide (LXC vs VM, GPU passthrough, headless Obsidian vault sync) |

## Harness reliability (branch `dgx`, 2026-11-16)

The harness, not the 2B model, carries reliability. Each step below is one commit; model-call cost per turn in brackets.

1. **Telemetry** [0 calls]: every guard, nudge, fast path, selector rule (WANTS/RECALL/NAMED), resolver,
   escalation and fake-tool detection counts its hits per turn (`hits` in the turn's trace) and in total
   (`harness_hits` in traces.db, kept even with `TRACE_DAYS=0`: names and counts only).
   `GET /v1/harness/stats` lists every counter with its last hit; `never_hit` names the candidates to prune.
2. **Inline tool-call recovery** [saves the nudge call]: a call written as text (`<tool_call>`, bare
   `{"name", "arguments"}`, `<function=..>` with JSON or `<parameter=..>`, `<function_call>`) is parsed
   (`agent/inline_calls.py`) and, if every call names a tool offered this turn and passes `_prepare` + `check`,
   run as a real call through hooks, policy, approval, taint and audit. Marked `recovered` in the trace;
   otherwise `FAKE_TOOL_NUDGE` as before. A reply starting as bare call JSON is no longer streamed.
3. **Actionable errors** [0 calls]: every `ERROR` the model can get back (≈85 in 16 modules) states what's wrong
   and the next step in a sentence starting Call/Ask/Answer/Tell/Use/Say/Give/Don't/Try/…; finite choices list the
   5 nearest (`guard/validate.py options()`: enums, timers, reminders, skills, pages, checks, tools). Errors whose
   text comes from elsewhere (exceptions, MCP servers, the bridge) get a plain next step at run time (`actionable()`).
   `test_every_error_names_the_next_action` scans the source so a new error can't skip it.
4. **Anti-loop** [0 calls]: a call identical to one already made this turn (`_signature`), or any call to a tool
   that has failed the same way twice this turn (same first sentence, values masked), gets
   `ERROR: same call again. Do not repeat it. Do the next step or answer.`; it never runs and is audited as
   `rejected`. A second such repeat ends the loop as before (escalate, or "going in circles").
5. **Live enums** [0 model calls; at most one audited GetLiveContext read a minute]: offered tools carry the
   current valid values of live arguments as a JSON-schema `enum` when there are 1–30 (running timer labels for
   cancel_timer, page names for read_page, skill names for the skill tools, Home Assistant device names for its
   Assist tools' `name`, by domain). Above 30, or with Claude (tool list kept stable), nothing is added and the
   resolvers work as before. The argument check holds the call to what was shown: a case slip is fixed, a value the
   resolver doesn't place gets the 5 nearest (timers, pages, skills; HA names pass through, as the cache can lag).
   HA names are read only when the policy lets GetLiveContext run unasked, are audited (`harness:live-enum`), and
   mark the turn as having read Home Assistant's data, so a change naming a device the user didn't say still asks.
   **Session refs** [0 calls]: background job ids reach the model as this conversation's refs (`ref_9f3c`,
   `guard/refs.py`), in tool output after the audit has kept the real text; a ref in a call is mapped back before
   the checks, the run and the audit. An unknown ref or a raw id gets an error naming the ref to use; a raw id the
   user said themselves (off the HUD) is accepted. Approval ids never reach the model, so they need no ref.
6. **Before-done contracts** [0 calls of their own]: what a turn must do is derived by code (`agent/contracts.py`):
   a web search asked for by name, a save ("remember it", "remember that…", a save part), one part of a
   request with several by its verb (timer, message, notes, a change), a fast-path action that failed, and a skill's
   hand-written `mandatory_tools: [{name, min_calls, on_missing: force|undone|ignore}]` (YAML frontmatter; part of the
   skill's pin, so adding it needs approval). Only calls that ran and succeeded count. A text reply with a contract
   unmet doesn't end the turn: the tool is forced (the existing constrained pick in this step), and if that doesn't
   do it, the reply is the truthful `UNDONE` line. A denial, timeout or policy block closes the contract; dry runs
   and Claude record it without enforcing. Each turn's trace lists its contract and whether it was met.
   Replaces the old `t.must` (explicit web, save part).
