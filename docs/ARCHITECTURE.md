# 🏛️ Architecture — poco-retail-ai-core

## 1. Module map

| Module | Responsibility | Key interfaces |
|---|---|---|
| `core/config.py` | Typed config from `config.env`/env vars | `Config.load()` |
| `core/main.py` | The 60s loop + lifecycle | `RetailAIDaemon.run_forever()` |
| `core/stream_manager.py` | Sequential RTSP grabbing | `grab_snapshot(url, camera_id)`, `snapshot_all(cameras)` |
| `core/inference_engine.py` | llama.cpp subprocess wrapper | `build_prompt(role, target)`, `run_inference(...)`, `parse_inference_output(text)` |
| `core/cloud_sync.py` | Channel-bus envelopes + firewall | `post_state(envelope)`, `poll_incoming()`, `is_subscription_active()` |
| `analytics/db_handler.py` | SQLite persistence | `save_telemetry(...)`, `get_hourly_counts()`, `get_peak_hours()` |
| `analytics/kpi_engine.py` | KPI math (pure) | `KpiEngine.calculate_hourly_metrics()` |

## 2. Data flow

```
RTSP cameras
    │  sequential open→grab→release (CAP_PROP_BUFFERSIZE=1)
    ▼
snapshots/<camera>_snapshot.jpg
    │  llama-minicpmv-cli -m models/<gguf> --image ... -p <role prompt>
    ▼
{count, staff_present, congestion, raw_text}
    │  db_handler.save_telemetry()
    ▼
SQLite telemetry_logs (WAL)
    │  hourly: kpi_engine.calculate_hourly_metrics()
    ▼
{current_status, last_analytics_report}
    │  cloud_sync.post_state() → sendMessage + pinChatMessage
    ▼
Telegram private state channel  ──►  dashboard (getChat.pinned_message.text)
    ▲
    │  getUpdates (channel_post / web_app_data)
Dashboard & admin panels (set_config / set_subscription envelopes)
```

## 3. Why the channel bus (design deviation)

The knowledge base specifies Bot API endpoints
`getCloudStorageValues` / `setCloudStorageValues` for core↔cloud sync.
**Verified against the official Bot API (2026-08): these methods do not
exist.** Mini App `CloudStorage` is accessible only from inside the Mini App
client, per user. A Python daemon cannot read or write it.

The working zero-cost equivalent, using only real Bot API methods:

| Need | Mechanism (verified in Bot API) |
|---|---|
| core → dashboard state | `sendMessage` (channel) + `pinChatMessage`; dashboard reads `getChat` → `pinned_message.text` |
| dashboard → core config | `sendMessage` (channel) → core sees `channel_post` via `getUpdates`; `web_app_data` (`sendData`) also handled as fallback |
| subscription firewall | `subscription_expiry` cached locally from envelopes; expiry → `Blocked` + inference paused |

The dashboard mirrors the pinned state into its own per-user CloudStorage
under the KB's keys (`current_status`, `last_analytics_report`, ...), keeping
the KB's CloudStorage key table meaningful client-side.

## 4. Decision log (from the knowledge base)

| Topic | Decision | Reasoning |
|---|---|---|
| Hardware | POCO F3 (SD870) | Cost/performance for MiniCPM-V |
| Environment | Termux + termux-services | Reliability, auto-restart |
| Memory | 2GB swap file | Prevents OOM kills |
| Persistence | Local SQLite → channel bus | Zero cloud cost; device keeps raw history |
| CCTV feed | Sequential grabber | No RAM spikes; real-time snapshots |
| Subscription | Admin panel → envelope → core | Centralized expiry; blocking firewall |
| Analytics | Footfall / Peak / Staffing / Congestion | Standard retail operational KPIs |
| Power | termux-wake-lock | Survives Android deep sleep |

## 5. Failure model

- Camera down → that camera skipped, others continue, logged.
- Inference crash/timeout → frame skipped, logged, loop continues.
- Telegram unreachable → envelopes dropped for this loop; SQLite keeps truth;
  next loop retries (backoff inside `api_call`).
- Subscription expired → `Blocked` status posted; inference paused; polling
  continues so renewal unblocks automatically.
- Malformed envelope → ignored with a debug log (never crashes the daemon).

## 6. Extension points

- Add camera roles in `inference_engine.ROLE_PROMPTS`.
- Add KPIs as pure methods on `KpiEngine` (they get unit-tested for free).
- Swap the inference backend by replacing `run_inference` (e.g. llama-server).
- Larger fleets: give each store its own channel; the dashboard reads the one
  configured in its Settings tab.
