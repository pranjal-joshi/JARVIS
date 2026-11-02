# JARVIS roadmap: built for a 2B model (November 2026)

Based on an architecture review of the hub on `jarvis-lfm3-gpu` (21 live read-only probes, token counts
measured against Ollama) and the evals run while building the voice and memory features.

## The one idea

With a frontier model, the model plans, acts and writes the reply; the hub is a safety harness.
With a ~2B model on a 2 GB GPU, that split doesn't work. **The model should classify and phrase.
The hub should plan, check, shape results and enforce style.** Everything below follows from that.

## What the review found (measured unless marked)

| Area | Finding |
|---|---|
| Context | System prompt alone ≈ 684 tokens; with the usual 9 tools ≈ 1,418; all 39 tools ≈ 3,900. Window is 8,192 (`compose.gpu-llm.yaml`). Tool results can each add ~1,500 tokens; nothing counts tokens, so a long turn can push the system prompt out (inferred). |
| Latency | Prompt eval 1.5–3 s for 1.4k tokens, 8.5 s for 3.9k. First words 6–15 s; two tools ≈ 17 s; +13 s cold load after 30 min idle (`OLLAMA_KEEP_ALIVE=30m`). The prompt prefix changes every minute (timestamp before the tools), so Ollama's prompt cache rarely helps (inferred). |
| Style | Bullets, bold and "Would you like…" tails on most multi-sentence replies, including on the satellite, despite the rules. On a real HA satellite the non-streamed path applies `for_ear` but not `to_speech`, so `- ` bullets would be read aloud. |
| Planning | Follows surface verbs: "weather where I live" asks which city, although `jarvis/location` has it. Explicit two-step requests work. |
| Maths/dates | "Days until 25 December" answered 63 (it's 53): it called `get_time` and did the subtraction itself. |
| Over-calling | `web_search` (approval + paid credit, 24 s) for "capital of Australia"; `search_notes` for general advice, then "no notes found" leaks into the reply. |
| Memory | Facts stated without "remember" are skipped; looked-up data is saved (`jarvis/phone` holds "battery 25%") and later recited as truth. `remember` is ungated, so tool output (web pages, notifications) can be persisted into memory and trusted later: an injection persistence path. |
| Search | `kb_search` has no relevance threshold; irrelevant memories enter every answer. |
| Small bugs | "Done, sir." when the model returns nothing (`tool_agent.py:139`). The spoken-yes parser accepted "OK, don't send it" (fixed in e0750e3). |

## Target architecture

```
utterance
  │
  ├─ 1. fast paths (code, no model)      time, timers, maths/date maths, weather <place>, my tasks,
  │                                       read page X, yes/no, status          → templated reply (<1 s)
  │
  ├─ 2. router (2B, JSON schema)          {intent, tool, args, missing[], confidence}
  │      ~300-token prompt, k tools picked by embeddings, one example per tool,
  │      Ollama `format` = schema, so the output is always valid
  │
  ├─ 3. composites (code)                 "where I live" → jarvis/location → weather
  │                                       "text <name>" → find_contact → send_text
  │
  ├─ 4. slot checks (code)                every name/number/date in args must occur in the
  │                                       utterance; writes read back before they commit
  │
  ├─ 5. tools via the existing gate       _approve → _run → audit (unchanged: every call,
  │                                       including code-made chains, goes through it)
  │
  ├─ 6. result shaping (code)             speakable sentences, relative dates ("tomorrow"),
  │                                       ≤80 words; many tools answer directly (final_answer)
  │
  ├─ 7. phrasing (2B, short prompt)       only when a tool result needs wording; persona-lite
  │
  ├─ 8. style + claim hooks (code)        strip lists/bold/tails, cap sentences per source,
  │                                       claim check, to_speech → for_ear for satellites
  │
  └─ cascade                              low confidence, missing slots or >2 steps →
                                          qwen3.5:4b on CPU, or Claude Haiku (external, gated)

memory: a separate pass after each turn extracts facts from the USER's words only (schema-
constrained), refuses text that came from a tool result, and records where each fact came from.
```

## Plan

### Phase 0: quick wins (about 1 week)

| # | Change | Fixes | Effort |
|---|---|---|---|
| 0.1 | Eval suite: ~60 fixed prompts (single intent, composite, memory, style, safety, voice) scoring tool choice, args, latency, style violations; one command per model, results in `docs/testing/` | Every later change is measured, not guessed | 1 day |
| 0.2 | Stable prompt prefix: timestamp and hints move into the user turn, tools in fixed order; `OLLAMA_KEEP_ALIVE=-1` + warm-up at hub start | 2–5 s per turn, no cold loads | hours |
| 0.3 | Style enforced in code: strip bullets/bold, drop "Would you like…/Let me know…" tails, cap sentences per source; satellite path gets `to_speech` before `for_ear`. Persona style block cut to 3 lines | Style violations, shorter prompt | hours |
| 0.4 | `kb_search` relevance threshold; "not for" lines on `web_search` (general knowledge) and `search_notes` (general advice) | Over-calling, irrelevant memories | hours |
| 0.5 | Token budget guard: trim history, then oldest tool results, so the system prompt always fits | Silent loss of the safety rules | half day |
| 0.6 | "Done, sir." on empty output → "Sorry sir, I didn't catch that." | False action claim | minutes |

### Phase 1: router architecture (2–3 weeks)

| # | Change | Fixes | Effort |
|---|---|---|---|
| 1.1 | Fast paths for closed intents, templated JARVIS replies | Latency (<1 s), date-maths errors | 1–2 days |
| 1.2 | JSON-schema router replacing free function calling for local models (Claude keeps native tools) | Invalid calls, faked tool markup, wrong tool | 2–3 days |
| 1.3 | Composite chains table (location, contacts, "how long until", "remind me") | Implied two-step tasks | 1 day |
| 1.4 | Slot post-conditions + read-back for every write | Changed dates/names in saves and sends | 2 days |
| 1.5 | Result shaping; `weather`, `get_time`, timers, tasks, `phone_status` answer directly | Second model call (3–8 s), "tomorrow" mislabels | 1 day |
| 1.6 | Cascade on low confidence (CPU 4B local first; Claude when a key exists, gated as external) | Hard questions on a small model | 1 day |
| 1.7 | Caching: weather 10 min, router decision per normalised utterance | Repeated voice commands | hours |

Targets (measured with 0.1): fast path < 1 s; routed single tool < 4 s to first word; two tools < 8 s;
tool choice ≥ 95 % on the suite; zero false action claims; zero style violations on the satellite.

### Phase 2: memory you can trust, and freedom with guard rails (2–3 weeks)

**Memory**
- 2.1 Extraction pass after each turn (user's words only, schema-constrained); provenance on every fact
  (said by you / approved by you / learned from a tool); tool-sourced text can't be saved without approval.
- 2.2 "Learned today" review in the HUD: keep, edit or forget each fact (also fixes stale ones like the battery level).

**Policy and hooks: more access, still guarded**
- 2.3 **Policy file** (`policy.yaml`, in the repo, not the KB) replacing the flat `APPROVAL_RISKS`: rules match
  tool, argument patterns, source, time of day and rate, and give `allow`, `ask`, `deny` or `dry-run`.
  Defaults reproduce today's behaviour (read allowed, external/destructive asked, hidden tools denied).
- 2.4 **Hooks**, declared in code: `pre_tool` (deny, ask, rewrite args, e.g. block a write whose text came
  from a web page), `post_tool` (verify the result, redact phone text before it reaches the audit),
  `pre_reply` (the style and claim checks move here), `on_event` (what skills use today). The existing guards
  become the first hooks, so there is one mechanism instead of several.
- 2.5 **Taint tracking**: text from web pages, notifications and messages is marked untrusted; a write or send
  whose arguments contain it asks first, whatever the policy says. This is what makes broader read access
  safe: JARVIS can read freely, but untrusted text can't quietly become an action or a memory.
- 2.6 **Wider read-only exploration**, each behind the policy: allow-listed personal folders (read-only),
  HA state, public web pages, phone read tools, with daily budgets (web credits, external calls, writes per hour).
- 2.7 **Dry-run for new skills**: an enabled skill first runs in dry-run, showing in the HUD what it would have
  done, until you promote it.
- 2.8 Persistent approval queue (survives restarts), and "allow this for 1 hour" on a card.

Unchanged: the token decides the source; satellites never approve; tool results stay fenced as data;
hidden tools stay hidden; work data stays out of scope unless you configure it.

### Phase 3: UI/UX (1–2 weeks, can overlap)

- 3.1 **"Why" panel per turn**: route taken (fast path, router, cascade), tools and arguments, confidence,
  time per stage. Makes small-model misses visible instead of mysterious.
- 3.2 Approval cards with the read-back sentence, a countdown, "allow for 1 h", and taint warnings.
- 3.3 Memory review page (2.2) and provenance badges in the knowledge-graph explorer.
- 3.4 Model badge with warm/cold state and recent latency; one-click cascade target.
- 3.5 Real phone push: a service worker so the HUD PWA gets approval notifications on Android
  (today only desktop browsers or the HA push can).
- 3.6 Eval dashboard: the 0.1 suite results per model and over time.
- 3.7 Satellite: "what can you do" spoken help, generated from the fast-path and router intents.

## Not doing (and why)

- Fine-tuning a router: no training data yet. Log router decisions for a month, then decide.
- Letting the 2B model plan multi-step tasks: composites in code are more reliable; `MAX_STEPS` stays a safety cap.
- Thinking mode, or a second model pass to fix style: too slow on this GPU (3–5 s each).
- Offering every tool to local models: 8.5 s of prompt evaluation measured.
- Multi-agent splits: more calls, more latency, no gain at this size.

## Sequencing

Phase 0 first (the eval suite makes everything after it measurable), then Phase 1. Phase 2 needs 1.2
and 1.4 in place. Phase 3 items can ship alongside whichever phase they explain.
