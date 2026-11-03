# Private Proactive Chat · Intensity + Humanization Edition

> A private-chat-only fork of [Pancakes-Labs/astrbot_plugin_proactive_chat](https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat).
>
> **Group proactive messages are fully removed.** No group listeners, no group timers, no group settings, no group UI.
> On top of the **interaction intensity** engine, v2.3.0 adds a humanization layer: circadian rhythm, relationship state, proactive intent, reply quality, a pre-send naturalness guard, bubble delivery plans, persona energy/mood and optional LivingMemory integration.
> v2.4.0 adds a native AstrBot plugin page (`views/proactive-chat`) that follows the Dashboard theme, while keeping the standalone WebUI.

---

## What it does

- Sends proactive messages to configured **private** conversations only, inside the user's learned active hours.
- Uses persona, conversation history / platform history, long-term memory, current time and unanswered count to generate content.
- Computes a 0–100 intensity score from:
  - message frequency (24h / 7d),
  - median reply tempo,
  - active days in the last 14 days,
  - reply rate / latency to previous proactive messages,
  - expressiveness (length, questions, exclamations, emoji).
- Applies freshness decay and a low-sample confidence factor.
- Maps the score to:
  - **trigger probability** (default 15% → 95%),
  - **effective interval** (default base 30–600 min × 0.35–1.15, with an 8-minute floor).
- Chooses **continue topic / new topic / care / share self / ask / stay silent** before generating text.
- Classifies replies as substantive / question / perfunctory / negative / media; negative feedback pauses proactive messages, and perfunctory/negative replies do **not** count as a reply to the previous proactive message.
- Runs a **pre-send naturalness guard** for repetition, length, too many questions, AI self-reference and customer-service tone.
- Sends 1–3 message bubbles with contextual delays, and only tries voice with a configured probability.
- Maintains a short-term **topic ledger** (open loops, user plans/events, unanswered questions) and a lightweight expression profile.
- Optionally uses **planner / writer / critic** multi-stage generation.
- Ships an **offline replay** tool to evaluate the policy against a real AstrBot database without sending messages.
- Supports quiet hours, daily budget, minimum gap, TTS and segmented replies.
- **Auto-pauses a user after several consecutive unanswered proactive messages**; on the next message an LLM topic check decides whether to resume (new topic) or stay paused (same topic). `resume_mode=any_message` is also available.
- Optionally recalls from and writes back to **LivingMemory**.
- Ships a standalone WebUI with a dedicated **Intensity** page and humanization badges.

---

## Quick start

1. Put this directory under `AstrBot/data/plugins/`.
2. Get the private UMO from logs or `/sid`:

   ```text
   qq_official:FriendMessage:<user_openid>
   ```

3. Configure:

   ```yaml
   private_settings:
     enable: true
     session_list:
       - "qq_official:FriendMessage:<user_openid>"
     schedule_settings:
       min_interval_minutes: 30
       max_interval_minutes: 600
       quiet_hours: "1-7"
       max_unanswered_times: 4
     intensity_settings:
       enable: true
   ```

4. Open the WebUI at `http://127.0.0.1:4100` and check **Status / Intensity / Tasks**.

Full Chinese documentation: [README.md](README.md), [docs/](docs/).

---

## Intensity in one formula

```text
raw   = 0.30*frequency + 0.22*tempo + 0.16*streak + 0.18*engagement + 0.14*expressiveness
score = clamp(100 * raw * freshness * volume, 0, 100)

probability = p_min + (p_max - p_min) * (score/100)^gamma
interval    = base_interval * (cold_scale - (cold_scale - hot_scale) * (score/100))
```

Default tiers: `dormant` (0–19), `steady` (20–39), `warm` (40–54), `close` (55–69), `blazing` (70–100).

See [docs/intensity.md](docs/intensity.md) for details.

---

## Calibration

An offline run against a local AstrBot database (read-only, no content uploaded):

- 784 private sessions, 14,600 user messages;
- median inter-message gap: 2 minutes; P75: 5 minutes; P90: 90 minutes;
- peak tier distribution: 278 dormant / 272 steady / 147 warm / 82 close / 5 blazing.

This separates one-off conversations from long-term high-frequency users.

---

## History seeding

```bash
python tools/import_astrbot_history.py \
  --db /path/to/data_v4.db \
  --report ./intensity_report.json

python tools/import_astrbot_history.py \
  --db /path/to/data_v4.db \
  --data-dir /path/to/plugin_data/astrbot_plugin_proactive_chat \
  --min-messages 20 \
  --write
```

The database is opened read-only; only private conversations are imported; `session_data.json` is backed up before writing.

---

## Tests

```bash
python -m pytest tests -q          # 180 tests
python -m compileall -q core main.py utils tools tests
python tools/simulate_intensity.py --all
cd tools && npm install && node check_jsx.js
```

---

## Diff from upstream

| Area | Upstream | This fork |
| --- | --- | --- |
| Group proactive | Supported | **Removed** |
| Group config | `group_settings` | Deleted |
| Group silence timer | Supported | Deleted |
| Config root | `friend_settings` | `private_settings` (auto-migrates) |
| Trigger probability | Fixed | Intensity-driven |
| Interval | Fixed random | Intensity-scaled |
| WebUI | Status/Tasks/Notifications/Docs/Config | Adds **Intensity** page, removes group UI |

License follows the original project. See [LICENSE](LICENSE).
