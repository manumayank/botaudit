# Reference Integration — mybot

This is a template integration. Copy this directory and adapt it for your bot.

## Files

| File | Purpose |
|------|---------|
| `audit_config.py` | Your bot-specific values: `BOT_ID`, `CLIENT_ID`, `PROJECT_ID`, paths, webhooks |
| `run_post_session.py` | CLI to process sessions, check DLQ, retry failures |
| `retrieve.py` | CLI to query project state, history, and free-form questions |

## Setup

1. Copy this directory: `cp -r examples/mybot mybot`
2. Edit `audit_config.py` — set `BOT_ID`, `CLIENT_ID`, `PROJECT_ID`, `SESSIONS_DIR`, `DB_PATH`
3. Put session JSONL files in `SESSIONS_DIR`
4. Run `python3 run_post_session.py --all`

## Usage

```bash
# Process all unprocessed sessions
python3 run_post_session.py --all

# Process one session
python3 run_post_session.py --session-file /path/to/session.jsonl

# Query state
python3 retrieve.py --status
python3 retrieve.py --history
python3 retrieve.py "What is pending?"

# DLQ
python3 run_post_session.py --dlq-status
python3 run_post_session.py --retry-dlq
```

## Session JSONL format

One JSON object per line. The platform handles both OpenClaw native format and generic format:

```json
{"role": "user", "content": "Update the checkout button to green", "timestamp": "2024-01-01T10:00:00Z"}
{"role": "assistant", "content": "Sure, I'll update the button color.", "timestamp": "2024-01-01T10:00:02Z"}
```
