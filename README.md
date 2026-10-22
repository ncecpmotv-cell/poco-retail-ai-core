# 📦 poco-retail-ai-core

**On-premise Edge-AI retail analytics daemon for Android/Termux.**

Turn a budget Android phone (POCO F3, Snapdragon 870) into a zero-cloud-cost
CCTV analytics box: it grabs frames from RTSP cameras, runs **MiniCPM-V**
vision inference through a locally compiled **llama.cpp** binary, stores every
observation in a local **SQLite** BI hub, computes retail KPIs, and publishes a
pinned JSON "state" to a **private Telegram channel** that the companion
Mini App ([`telegram-retail-ai-dashboard`](https://github.com/ncecpmotv-cell/telegram-retail-ai-dashboard))
reads.

> 📄 Full system context: see the project knowledge base and the companion
> dashboard repo. This repo is the **edge half**; the dashboard is the **UI half**.

---

## 🏗️ Architecture

```
┌───────────────────────────── POCO F3 (Termux) ─────────────────────────────┐
│                                                                            │
│  RTSP cam 1 ─┐                                                             │
│  RTSP cam 2 ─┼─► stream_manager (sequential, BUFFERSIZE=1) ── snapshot ─┐  │
│  RTSP cam N ─┘                                                          │  │
│                                                                         ▼  │
│                              inference_engine ── llama-mtmd-cli        │
│                                   │  (native C++ subprocess)               │
│                                   ▼                                        │
│                         analytics/db_handler ──► SQLite (WAL)              │
│                                   │                                        │
│                                   ▼                                        │
│                         analytics/kpi_engine ──► Footfall, Peak Hours,     │
│                                                  Staffing Gaps,            │
│                                                  Congestion Index          │
│                                   │                                        │
│                                   ▼                                        │
│                         core/cloud_sync ── sendMessage + pinChatMessage ─┐ │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┼─┘
                                                                             │
                        ┌────────────────────────────────────────────────────▼──┐
                        │  PRIVATE TELEGRAM "STATE" CHANNEL (zero-cost bus)     │
                        │  pinned state envelope ◄── getChat ── Telegram        │
                        │  set_config / set_subscription envelopes ──► getUpdates│
                        └───────────────────────────────────────────────────────┘
```

### The 60-second loop (`core/main.py`)

```
sync config (getUpdates) → subscription firewall → snapshot each camera →
MiniCPM-V inference → save telemetry (SQLite) → hourly KPIs → post/pin state
```

- **Sequential camera processing** — one camera at a time; a dead camera never
  kills the loop.
- **Subscription firewall** — when `subscription_expiry` is in the past, the
  daemon sets status `Blocked`, pauses inference, but keeps polling so a
  renewal can unblock it automatically.
- **Local-first / offline-tolerant** — every inference result lives in SQLite;
  cloud post failures are logged and retried next loop.

### Envelope schema (channel bus)

All channel messages are JSON envelopes. Field names keep the knowledge-base
CloudStorage keys, so the dashboard's CloudStorage mirror stays compatible.

| Direction | `type` | Payload |
|---|---|---|
| core → dashboard (pinned) | `state` | `{updated_at, subscription_expiry, current_status: {status, count, alert, camera_id, congestion_index, timestamp}, last_analytics_report: {peak_hour, total_visits, efficiency, footfall_today, footfall_yesterday, trend_pct_vs_yesterday, staffing_gaps, congestion_index, congestion_level, current_count, peak_hour_distribution, generated_at}}` |
| dashboard → core | `set_config` | `{user_id, payload: {cctv_rtsp_urls: [{id, url, role?}], ai_target_prompt}}` |
| admin → core | `set_subscription` | `{user_id, payload: {user_id, subscription_expiry}}` |

> ⚠️ **Design deviation from the knowledge base.** The KB proposes Bot API
> methods `getCloudStorageValues` / `setCloudStorageValues`. Those methods
> **do not exist** in the Telegram Bot API (CloudStorage is client-side only).
> This repo implements the verified working zero-cost equivalent — the private
> channel bus above. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for details.

---

## 📋 Prerequisites

| Item | Requirement |
|---|---|
| Phone | Android 10+, ≥6GB RAM recommended (tested target: POCO F3 / SD870) |
| Apps | [Termux](https://f-droid.org/packages/com.termux/) (F-Droid build), [Termux:API](https://f-droid.org/packages/com.termux.api/), [Termux:Boot](https://f-droid.org/packages/com.termux.boot/) (optional, auto-start) |
| Storage | ~5GB free (toolchain + llama.cpp + ~3GB GGUF model) |
| Telegram | A bot token from [@BotFather](https://t.me/BotFather) |
| Network | LAN access to the RTSP cameras; internet for Telegram sync |

---

## 🚀 Installation (step by step)

### 1. Prepare Termux

```bash
# Allow storage & disable battery optimizations for Termux in Android settings.
termux-setup-storage
pkg update -y
```

### 2. Clone and run the setup script

```bash
git clone https://github.com/ncecpmotv-cell/poco-retail-ai-core.git
cd poco-retail-ai-core
chmod +x setup.sh && ./setup.sh
```

`setup.sh` is idempotent and does everything:

1. Installs `git cmake clang make python opencv termux-api termux-services`.
2. Creates a **2GB Linux swap file** (prevents Android OOM kills during
   inference) and persists it in `~/.bashrc`.
3. Creates a Python venv and installs `requirements.txt`.
4. Clones and builds **llama.cpp** with native Snapdragon 870 flags
   (`-march=armv8.2a+dotprod -O3`), producing `llama-mtmd-cli`.
5. Downloads **MiniCPM-V 4.7B GGUF** (~3GB) from Hugging Face into `models/`.
   *If the filename 404s, check the file listing at
   <https://huggingface.co/openbmb/MiniCPM-V-4.7-1B-gguf> and re-run with
   `MODEL_FILE=<actual-name> ./setup.sh`.*
6. Creates `config.env` from the template and the `termux-services` unit.
7. Acquires the **wake lock** so Android never freezes the daemon.

### 3. Configure

```bash
nano config.env
```

| Key | What to put |
|---|---|
| `BOT_TOKEN` | Token from @BotFather |
| `STATE_CHANNEL_ID` | Your private channel: `@username` or numeric id (see below) |
| `ADMIN_ID` / `USER_ID` | Your Telegram numeric ids |
| `STORE_CAPACITY` | Max people before `Critical` congestion |
| `STAFFING_THRESHOLD` | People count above which missing staff = staffing gap |
| `LOCAL_CAMERAS` | JSON list `[{"id":"cam1","url":"rtsp://user:pass@ip:554/stream","role":"entrance"}]` (fallback until the dashboard pushes its config) |

**Getting ids:** message [@userinfobot](https://t.me/userinfobot) for your user
id. For a channel id, create the channel, forward any of its posts to
[@userinfobot](https://t.me/userinfobot), or just use its `@username`.

### 4. Create the Telegram bridge (once)

1. Create a **private channel** (e.g. `MyStoreRetailAI`).
2. Add your (main) bot as **administrator** with **Post messages** and
   **Pin messages** rights — the daemon uses it to post/pin state and to poll
   `getUpdates`.
3. Settings from the dashboard reach the daemon through the bot's
   `getUpdates` stream via **`sendData`** (`web_app_data` updates — primary,
   one bot, no extra setup). If you also want channel-post writes for
   link-launched apps, create an optional second **relay bot** in
   [@BotFather](https://t.me/BotFather) and add it to the channel with the
   same rights; the dashboard then uses the relay token. (Telegram never
   echoes a bot's own channel posts back to itself, which is why that path
   needs the second bot.)
4. Put the channel's `@username` (or numeric id) into `STATE_CHANNEL_ID`.
5. That's the entire backend — Telegram's servers are the free state bus.

### 5. Start the daemon

```bash
sv-enable retail-ai   # start now + on boot
sv status retail-ai   # check it is "run"
tail -f logs/service.log
```

Useful runit commands: `sv up retail-ai`, `sv down retail-ai`,
`sv restart retail-ai`.

### 6. Optional quick sanity test (no cameras needed)

```bash
.venv/bin/python - <<'EOF'
from core.config import Config
cfg = Config.load()
print("token set:", bool(cfg.bot_token), "| cameras:", len(cfg.local_cameras))
EOF
```

---

## 🧪 Tests & development

```bash
python3 -m unittest discover -s tests -v   # stdlib-only, no pip installs needed
python3 -m py_compile analytics/*.py core/*.py tests/*.py
```

CI: GitHub Actions (`.github/workflows/tests.yml`) runs the same suite on every
push and pull request (Python 3.10 & 3.12). The `analytics/` package and tests
deliberately import **only the standard library**, so you can validate KPI
math anywhere, even without OpenCV/requests.

---

## 📐 KPIs implemented

| KPI | Definition |
|---|---|
| **Footfall** | Sum of counted people per hour / per day |
| **Peak Hours** | Hours of day ranked by traffic |
| **Staffing Gap** | `count > threshold` AND `staff_present == false` |
| **Congestion Index** | `current people / store capacity` → `Normal / Congested / Critical` |

---

## 🔐 Security notes

- `config.env` and `data/` are gitignored — never commit the bot token.
- The dashboard embeds the same bot token client-side to read the pinned state
  (there is no zero-cost way to hide it). Mitigations: use a **dedicated bot**
  for this system, keep the state channel **private**, rotate the token via
  @BotFather `/revoke` if exposed.
- The `set_subscription` envelope is only honored when its `user_id` equals
  `ADMIN_ID` (trust-the-private-channel model — documented limitation).

## 🧯 Troubleshooting

| Symptom | Fix |
|---|---|
| Android kills the daemon | Check `free -h` — swap should show 2GB; `sv restart retail-ai`; disable battery optimization for Termux |
| `camera ... could not open stream` | Verify RTSP URL in VLC; some cameras need `rtsp_transport tcp` — add `?tcp` handling or a lower-resolution substream |
| Inference binary not found | Re-run `./setup.sh` (step 4 builds llama.cpp) |
| Model download fails (401/404) | Verify `MODEL_FILE`/`MMPROJ_FILE` against the [HF repo](https://huggingface.co/openbmb/MiniCPM-V-4.7-1B-gguf) file listing |
| `Telegram API unreachable` | Check token, network; Telegram is blocked in some regions (consider a proxy) |
| `could not pin state message` | Grant the bot **Pin messages** admin right in the channel |
| Status stuck at `Blocked` | An admin must set a future `subscription_expiry` from the dashboard |

---

## 📁 Repository layout

```
poco-retail-ai-core/
├── setup.sh                  # one-shot Termux bootstrap (idempotent)
├── config.env.example        # configuration template
├── requirements.txt          # opencv-python, requests, python-dotenv
├── analytics/
│   ├── db_handler.py         # SQLite BI hub (telemetry_logs + queries)
│   └── kpi_engine.py         # Footfall / Peak / Staffing / Congestion
├── core/
│   ├── config.py             # typed config loader
│   ├── stream_manager.py     # sequential RTSP snapshot grabber
│   ├── inference_engine.py   # llama-mtmd-cli wrapper + output parser
│   ├── cloud_sync.py         # channel-bus envelopes + subscription firewall
│   └── main.py               # the 60s daemon loop
├── services/retail-ai.service# termux-services (runit) unit
├── tests/                    # unittest suite (stdlib only)
├── .github/workflows/tests.yml # CI — unittest suite on every push
├── docs/ARCHITECTURE.md      # module map + design decisions
└── config.env.example        # configuration template
```

## 📄 License

MIT — see [LICENSE](LICENSE).
