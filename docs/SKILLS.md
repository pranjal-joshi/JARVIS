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
| `notification` | a new notification on an allowed phone (WhatsApp, SMS, mail, any app; polled every 10 s) | "App: title — text" |
| `call_incoming` | an allowed phone starts ringing | caller number and phone model |
| `schedule` | `schedule:: 07:30` (daily) or `schedule:: every 30m` / `every 2h` | – |
| `watch` | a watcher (HUD → **watch**) finds a new item | "watcher name: item text" |
| `webhook` | something POSTs to `/v1/hooks/<name>` (see below) | the hook name only, exactly (not the body) |

HUD header → **skills**: every skill page with its trigger, **enable** / **on**, and **run** (test it now).
Events (hooks) show in the system console (⚡) and in the panel's recent-events list; a skill's answer
appears in the chat marked ⚙ and is spoken when voice is on.

## Webhooks

Any service that can send an HTTP request can start a skill: a Home Assistant automation, GitHub or Azure
DevOps service hooks, IFTTT/Zapier, a cron job.

```
trigger:: webhook
match:: github

- Tell me in one sentence what happened on GitHub.
```

- **Turn it on:** HUD → ⚙ → **webhooks** → *turn on* makes a secret (or set `HUB_WEBHOOK_SECRET`; then the
  HUD shows it but can't rotate it). No secret = `/v1/hooks/*` answers 404.
- **Call it:** `POST https://<hud>/api/hub/hooks/<name>` (through the HUD's proxy) or
  `http://hub:8700/v1/hooks/<name>` from another container, with `X-Jarvis-Token: <secret>` (or
  `Authorization: Bearer <secret>`). GitHub can instead sign the body with the secret (`X-Hub-Signature-256`).
- The body (JSON or text, ≤ 64 KB) becomes one line ("github (pull_request): action=closed;
  pull_request.title=…") that the skill sees **as data**. `X-GitHub-Event` / `X-Jarvis-Event` adds the event name.
- 20 calls a minute per hook, 60 in all. The panel shows the last deliveries (refused ones too, never the
  body), the skills listening, a ready-made curl, and **fire test** (runs the skills without the secret).
- The secret is not a hub client token: it can't chat, approve or read anything.

## Safety

- KB pages are data. A skill never runs because a page exists: you enable it in the HUD, which pins
  the page's content hash. Edit the page and it switches itself off ("edited · enable") until you
  enable the new version.
- A run is an ordinary agent turn from the HUD: every tool call is audited, and destructive or
  external tools still wait for your approval (and time out if you're away).
- One run per skill at a time. Enabled skills live in `skills.json` next to the hub database.
