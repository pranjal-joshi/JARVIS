# LFM3 tool-calling SFT set from JARVIS traces

From 834 traced turns: **270 samples** (244 train, 26 val).

| | |
|---|---|
| samples before de-duplication | 470 |
| near-duplicates dropped (same request and calls, over 2) | 200 |
| samples with a repaired step | 10 |
| corrected turns (review.json fixes) | 21 |
| samples from turns since 5 Nov 2026 (of them corrected) | 54 (21) |
| samples ending at a tool call (dry run or bridge down) | 94 |
| samples answering without a tool | 41 |
| graded by an eval case / reviewed by hand (review.json) | 167 / 103 |

## Turns left out

- no model step (fast path, memory): 163
- tainted (outside text read): 37
- unusable reply: 28
- missed part: 22
- failed call: 14
- skipped: 8
- wrong tool: 8
- review: ran a dream for a question: 4
- review: no answer: 4
- review: answered without searching: 3
- review: never searched the web: 2
- review: contradicting reply: 2
- review: unsaid-claim line, no answer: 2
- review: skill run switched itself live: 2
- args: 2
- review: skips 'hello world', wrong greeting: 2
- review: skips 'hello world': 2
- review: repeats a wrongly saved fact: 2
- review: odd reply: 1
- review: reply repeats itself: 1
- review: wrong fact in reply (Ghidra is Java): 1
- review: didn't play anything: 1
- review: no tool: phone news needs phone_notifications: 1
- review: model didn't answer: 1
- review: notification instead of a daily reminder: 1
- review: says recurring reminders are impossible: 1
- review: claims a timer it never set: 1
- review: 12-hour timer instead of a reminder: 1
- review: garbled page text: 1
- review: needless get_time, nothing saved: 1
- review: asks for a URL instead of searching: 1
- review: claims a save it didn't make: 1
- review: duplicate page writes: 1
- reply claims a journal write: 1
- review: promised to save, didn't: 1
- review: journal write for 'save it' (now remember): 1
- review: needless get_time, nothing done: 1
- review: write refused, rambling reply: 1
- review: reminder for a link to remember: 1
- review: searched, never saved: 1
- review: statement not saved: 1
- review: asked instead of searching: 1
- review: answers 'thanks' with a reminder offer: 1
- review: claims an update it never made: 1
- review: skill run calls run_skill: 1
- review: old enable_skill arguments: 1
- review: skill run switched itself on: 1
- review: skill run not done: 1
- review: made-up weekday: 1
- review: wrong age: 1
- review: rambling age reply: 1
- review: correction never saved: 1
- review: works the age out itself, rambling reply: 1
- review: fact not saved: 1
- review: leaks the system prompt: 1
- review: list_skills for a question about jobs: 1
- review: fake tool text in reply: 1
- review: contradicts what the user said: 1
- review: journal write for 'save them' (now remember): 1
- review: deleted pages, then a wrong reply: 1
- review: asks to delete the wrong page: 1
- review: no undo, wrong reply: 1
- review: correct_memory for a delete request: 1
- review: asks Claude to delete files: 1
- review: five duplicate remembers: 1
- review: needless notes search for a greeting: 1
- review: muddled reply: 1
- review: unrelated facts given as 'dreams': 1
- review: says it can't read the notes: 1
- review: skill steps are the request itself: 1
- review: garbled joke: 1
- review: arguments the harness had to fix: 1
- review: garbled table reply: 1
- review: remember for a question: 1
- review: talks about itself in the third person: 1
- review: write refused, wrong reply: 1
- review: saved the work fact, not the web info asked for: 1
- review: named-page save with nothing from before to save: 1
- review: asks the user to dictate the fact: 1
- review: duplicate writes, contradicting reply: 1
- review: asks what a stated job is: 1
- review: writes refused, claims a journal entry: 1
- review: worked the years out itself, wrongly: 1
- review: left the wrong memory in place: 1
- review: timeline invents an end year: 1
- review: missed the user's own jobs: 1
- review: no pages listed: 1
- review: thin answer: 1
- review: harness-picked save of a plain statement: 1
- review: searched the same word again: 1
- review: unclear: the interior note was saved garbled, nothing to answer from: 1
- review: searched instead of taking a stated fact: 1
- review: harness-picked save repeating a fact: 1
- review: offers instead of doing: 1
- review: says it can't browse instead of searching: 1
- review: journal write for a plain save: 1
- review: new page for a plain save: 1
- review: duplicate writes: 1

## Repairs

- guard-refused call: 20
- nudge: 3

## Samples by the model that produced them

- jarvis-lfm3-gpu: 213
- groq-qwen3.8-27b: 21
- groq-gpt-oss-20b: 18
- unsloth-lfm3-sft: 10
- groq-gpt-oss-120b: 7
- zai-glm-4.7-flash: 1

## Tool calls taught

- search_notes: 75
- remember: 28
- web_search: 17
- start_job: 12
- media_control: 10
- list_tasks: 9
- set_timer: 8
- laptop_status: 7
- read_page: 5
- get_time: 5
- ask_claude: 4
- phone_notifications: 4
- add_to_page: 4
- add_to_journal: 4
- phone_status: 4
- send_notification: 4
- send_text: 4
- set_reminder: 3
- read_web_page: 3
- list_reminders: 3
- list_timers: 3
- find_contact: 3
- move_note: 2
- create_skill: 2
- list_skills: 2
- read_texts: 2
- recent_calls: 2
- job_status: 2
- list_phones: 2
- phone_screenshot: 2
- powershell_read: 2
- list_files: 2
- open_on_laptop: 2
- cancel_timer: 2
- laptop_screenshot: 2
- lock_laptop: 2
- enable_skill: 1
- run_skill: 1
- dream: 1

## Notes

Written by hand; the build keeps this section when it rewrites the tables above.

### 14 Nov 2026 rebuild: traces of 5-14 Nov added

The first build (222 samples: 201 train, 21 val) covered the 643 turns of 3-4 Nov. This one adds the 191 turns
since (5 Nov: 55, 12 Nov: 17, 13 Nov: 106, 14 Nov: 13; the last ones from the hub running on the host,
`data/hub-local/traces.db`): **270 samples** (244 train, 26 val), 54 of them from the new turns.

- **33 new turns kept as they were** after review: notes lookups (including honest misses), `remember` with the
  user's words, `list_skills`, `list_reminders`, `create_skill` on a webhook, `set_timer`, `ask_claude` on
  request, and answers that need no tool.
- **21 turns corrected** (`fix` in `review.json`) where the right call and reply are plain from the notes and
  tool outputs; nothing in them is new to the trace except the corrected step:
  - "save it" / "save that" / "did you save the above plan" / "save the earlier plan Claude gave" save what came
    before (the user's words, Claude's plan) with `remember`, instead of asking the user to retype it (13 Nov
    23:40), saving only the plan's title, or saving the model's own summary ("BEL from 2018 until now");
  - "What about my relationships?" searches again (for the wife the system prompt already names) before saying
    there is nothing; the second search's result is the real one from the same search a minute later;
  - "why did you…" / "why would this happen?" get what was done and "I don't know", not a guessed cause;
  - tool failures (a background job that couldn't start) said in plain words, without "expert bridge";
  - replies after `remember` that claimed a journal copy, an unsaid extra save, or called a note a "reminder";
  - a topic outside the fixed pages (`user: 'Remember about home'` → `home`), a bare username saved without
    what it is, a needless notes search for a general-knowledge question.
- **137 new turns left out**: 37 tainted (outside text read, all skill runs included), 28 fast path or memory
  capture, 26 unusable replies ("going in circles", "haven't actually saved…", "the language model is not
  answering", now in `BAD_REPLY`), 4 failed calls, 42 by review (dreams run for questions, answers without
  searching, harness-picked or duplicate writes, repeats of a fact that was saved wrongly, journal or new pages for
  a plain save, …).
- **Old samples that are wrong targets now**: "save it" / "save them" answered with a journal write (2 turns),
  a rambling age sum worked out by hand (1), a reply claiming a journal copy of a memory (1), and a two-part save
  whose reply ends in "the language model is not answering" (1, now caught by `BAD_REPLY`) are out: 222 → 216.
  "and today's journal" is taken out of every old `remember` / `correct_memory` result.
- From 5 Nov on, tainted turns, skill runs and dry runs are left out (`STRICT_SINCE`); the first build's turns
  keep their old rules, since its eval runs read web pages in dry runs and dropping them would lose most of the
  graded web-search and tool-choice samples.

Not turned into examples, because the right answer isn't plain from the trace: "Who's my interior" (the
interior designer was saved garbled by memory capture, so the notes really didn't have it), "save this to Sanika
page" (the CEO names it meant came two turns earlier, behind a refused search), "Find a good Dreame vacuum on
Amazon" (no search results to answer from), and statements made without "remember" (memory capture handles those
in code now).

The hub still returns `Saved "…" to [[page]] and today's journal.` from `remember` (`hub/src/jarvis_hub/kb/tools.py`,
`kb_remember`), though it no longer writes the journal copy; the samples use the text without it.
