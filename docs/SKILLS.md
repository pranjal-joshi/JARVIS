# Skills and hooks

A **skill** is a Logseq page under `skills/` that JARVIS runs when an event fires. The page
properties say when; the blocks are the instructions.

```
trigger:: device_joined
match:: Pixel

- A phone just connected. Tell me its model and battery level in one sentence.
```

| trigger | fires when | `match::` is checked against |
|---|---|---|
| `device_joined` / `device_left` | an Android device connects / disconnects over ADB (polled every 10 s via the device bridge) | model and serial |
| `timer_done` | a `set_timer` timer ends | the timer label |
| `schedule` | `schedule:: 07:30` (daily) or `schedule:: every 30m` / `every 2h` | – |

HUD header → **skills**: every skill page with its trigger, **enable** / **on**, and **run** (test it now).
Events (hooks) show in the system console (⚡) and in the panel's recent-events list; a skill's answer
appears in the chat marked ⚙ and is spoken when voice is on.

## Safety

- KB pages are data. A skill never runs because a page exists: you enable it in the HUD, which pins
  the page's content hash. Edit the page and it switches itself off ("edited · enable") until you
  enable the new version.
- A run is an ordinary agent turn from the HUD: every tool call is audited, and destructive or
  external tools still wait for your approval (and time out if you're away).
- One run per skill at a time. Enabled skills live in `skills.json` next to the hub database.
