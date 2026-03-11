# Bot Audit Platform

A lightweight, production-grade audit, governance, and state platform for client-facing AI bots.

Built for teams who need to know: *what did the bot do, what did the client ask for, what is pending, and did it go to production safely?*

---

## What It Does

When your AI bot talks to clients, this platform:

1. **Tracks sessions** — one session per client conversation, with idle/max-duration boundaries
2. **Extracts structured audit records** — after each session closes, a second LLM pass extracts what was asked, decided, done, and pending (with evidence anchors back to the transcript)
3. **Materializes project state** — a live view of `active_requests`, `awaiting_client_review`, `deployed_changes`, `next_actions` — rebuilt from the append-only event log
4. **Routes code changes safely** — three-tier execution: bash scripts for simple changes, Claude Code for intelligent changes, Claude Code + Codex review for risky changes
5. **Handles failures gracefully** — dead letter queue preserves every transcript even when downstream processing fails

---

## Architecture (v4)

Four systems, clear roles:

```
┌─────────────────────────────────────────────────────────────┐
│  OpenClaw / Your Bot (LLM)                                  │
│  Role: READS state, classifies, extracts, builds handoffs   │
│  Never writes directly to the audit store                   │
└────────────────────┬────────────────────────────────────────┘
                     │ handoff payload (JSON)
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  n8n (Orchestrator)                                         │
│  Role: WRITES events, updates state, routes to workers      │
│  Receives handoffs from bot, triggers Claude Code / bash    │
└──────┬──────────────────────────────┬───────────────────────┘
       │                              │
       ▼                              ▼
┌──────────────┐              ┌──────────────────────────────┐
│ Claude Code  │              │ Codex (OpenAI)               │
│ Code changes │ ──diff──────▶│ Independent review           │
│ via CLI      │              │ Different vendor = real       │
└──────────────┘              │ N-version verification       │
                              └──────────────────────────────┘
```

**Data access rule:** The bot reads state. n8n writes state. This is a hard boundary.

See [ARCHITECTURE.md](bot-audit-platform/ARCHITECTURE.md) for the full technical design.

---

## Key Features

- **Append-only event log** — events are never updated or deleted; idempotency-keyed
- **Second-pass extraction** — audit records come from a separate LLM prompt, not the bot summarizing itself
- **Evidence anchors** — every extracted claim links to source message IDs + timestamps
- **Optimistic locking** — concurrent sessions can't corrupt project state
- **Policy-based retrieval** — deterministic lookup (project_state → audit summaries → transcripts → uncertainty)
- **Dead letter queue** — failures are preserved with exponential backoff retry
- **Three-tier execution routing** — `action_type + risk_level → bash | claude_code | claude_code + codex`
- **Human override events** — every AI decision override is audited with mandatory reason field
- **Zero external dependencies** — Python stdlib only (`urllib`, `sqlite3`, `json`)

---

## Project Structure

```
bot-audit-platform/
├── bot_audit/
│   └── audit/              # Core platform
│       ├── config.py       # Default configuration
│       ├── db.py           # SQLite schema (7 tables)
│       ├── session.py      # Session lifecycle manager
│       ├── classifier.py   # Intent + action_type + risk_level classifier
│       ├── llm.py          # OpenRouter API wrapper (stdlib urllib)
│       ├── extractor.py    # Second-pass LLM audit extraction
│       ├── materializer.py # Project state materialized view
│       ├── retrieval.py    # Policy-based retrieval ladder
│       ├── dead_letter.py  # Dead letter queue
│       ├── pipeline.py     # Post-session orchestrator
│       ├── handoff.py      # Handoff payload builders (OpenClaw → n8n)
│       ├── routing.py      # Three-tier execution routing table
│       └── codex.py        # Codex review integration
├── examples/
│   └── mybot/              # Reference integration — copy and adapt
├── scripts/
│   └── claude-worker.sh    # n8n worker wrapper for Claude Code
├── ARCHITECTURE.md
├── QUICKSTART.md
├── N8N_SETUP.md
└── requirements.txt
```

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/manumayank/botaudit
cd botaudit/bot-audit-platform

# 2. No pip install needed — zero external dependencies
#    Python 3.9+ required

# 3. Set your OpenRouter API key
export OPENROUTER_API_KEY=sk-or-...

# 4. Copy the example integration
cp -r examples/mybot mybot
cd mybot

# 5. Edit audit_config.py with your bot/client/project IDs

# 6. Process your first session
python3 run_post_session.py --session-file /path/to/session.jsonl

# 7. Query the state
python3 retrieve.py "What is pending?"
python3 retrieve.py --status
python3 retrieve.py --history
```

See [QUICKSTART.md](bot-audit-platform/QUICKSTART.md) for the full walkthrough.

---

## Requirements

- Python 3.9+
- An LLM API key — [OpenRouter](https://openrouter.ai) (recommended) or OpenAI-compatible endpoint
- n8n (for the full automation pipeline) — see [N8N_SETUP.md](bot-audit-platform/N8N_SETUP.md)
  - n8n requires Node 20 (does not work on Node 25)
  - Install: `nvm install 20 && nvm use 20 && npm install -g n8n`

---

## Database Schema

SQLite with WAL mode. Seven tables:

| Table | Purpose |
|-------|---------|
| `events` | Append-only event log (source of truth) |
| `session_state` | One row per session; unique index enforces one open session per bot+client |
| `project_state` | Materialized view, updated via optimistic locking |
| `dead_letter` | Failed processing items with retry state |
| `review_events` | Denormalized Codex review verdicts for fast query |
| `pipeline_pause` | Operator-controlled pause/resume per scope |
| `hold_queue` | Handoffs held while pipeline is paused |

---

## Three-Tier Execution Routing

Routing is deterministic — `action_type + risk_level` maps to a worker:

| action_type | risk_level | worker | codex_review |
|-------------|-----------|--------|-------------|
| `ui_copy_change` | low | bash | no |
| `config_change_safe` | low | bash | no |
| `ui_style_change` | low | claude_code | no |
| `ui_style_change` | medium | claude_code | yes |
| `logic_change` | medium/high | claude_code | yes |
| `data_model_change` | high | claude_code | yes |
| `api_change` | high | claude_code | yes |
| `bug_fix` | any | claude_code | yes (always) |
| `deploy` / `rollback` | any | n8n_direct | no |

---

## License

MIT
