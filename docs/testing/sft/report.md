# LFM3 tool-calling SFT set from JARVIS traces

From 643 traced turns: **222 samples** (201 train, 21 val).

| | |
|---|---|
| samples before de-duplication | 421 |
| near-duplicates dropped (same request and calls, over 2) | 199 |
| samples with a repaired step | 11 |
| samples ending at a tool call (dry run or bridge down) | 94 |
| samples answering without a tool | 30 |
| graded by an eval case / reviewed by hand (review.json) | 167 / 55 |

## Turns left out

- no model step (fast path, memory): 135
- missed part: 22
- failed call: 10
- skipped: 8
- wrong tool: 8
- review: no answer: 4
- review: never searched the web: 2
- review: unsaid-claim line, no answer: 2
- review: skill run switched itself live: 2
- args: 2
- review: skips 'hello world', wrong greeting: 2
- review: skips 'hello world': 2
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
- review: promised to save, didn't: 1
- nothing left to learn: 1
- unusable reply: 1
- review: needless get_time, nothing done: 1
- review: write refused, rambling reply: 1
- review: reminder for a link to remember: 1
- review: contradicting reply: 1
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
- review: ran a dream for a question: 1
- review: rambling age reply: 1
- review: correction never saved: 1
- review: fact not saved: 1
- review: leaks the system prompt: 1
- review: list_skills for a question about jobs: 1
- review: fake tool text in reply: 1
- review: contradicts what the user said: 1
- review: deleted pages, then a wrong reply: 1
- review: asks to delete the wrong page: 1
- review: no undo, wrong reply: 1
- review: correct_memory for a delete request: 1
- review: asks Claude to delete files: 1
- review: five duplicate remembers: 1

## Repairs

- guard-refused call: 20
- nudge: 3

## Samples by the model that produced them

- jarvis-lfm3-gpu: 193
- groq-qwen3.8-27b: 21
- groq-gpt-oss-120b: 7
- zai-glm-4.7-flash: 1

## Tool calls taught

- search_notes: 54
- web_search: 17
- remember: 16
- start_job: 10
- media_control: 10
- list_tasks: 9
- laptop_status: 7
- set_timer: 7
- add_to_journal: 6
- read_page: 5
- get_time: 5
- phone_notifications: 4
- add_to_page: 4
- phone_status: 4
- send_notification: 4
- send_text: 4
- set_reminder: 3
- read_web_page: 3
- list_timers: 3
- find_contact: 3
- move_note: 2
- read_texts: 2
- recent_calls: 2
- list_reminders: 2
- job_status: 2
- ask_claude: 2
- list_phones: 2
- phone_screenshot: 2
- powershell_read: 2
- list_files: 2
- open_on_laptop: 2
- cancel_timer: 2
- laptop_screenshot: 2
- lock_laptop: 2
- create_skill: 1
- enable_skill: 1
- run_skill: 1
- correct_memory: 1
- dream: 1
