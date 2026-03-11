# Quickstart Guide

## Prerequisites

- Python 3.9+
- An [OpenRouter](https://openrouter.ai) API key (or any OpenAI-compatible endpoint)
- No pip install required — zero external dependencies

---

## Step 1: Clone and verify

```bash
git clone https://github.com/your-org/bot-audit-platform
cd bot-audit-platform

# Verify Python version
python3 --version  # needs 3.9+
```

---

## Step 2: Set your API key

```bash
export OPENROUTER_API_KEY=sk-or-your-key-here
```

Or create a local config file at `~/.bot_audit/config.json`:
```json
{
  "env": {
    "OPENROUTER_API_KEY": "sk-or-your-key-here"
  }
}
```

---

## Step 3: Copy the example integration

```bash
cp -r examples/mybot mybot
cd mybot
```

Edit `audit_config.py` with your values:

```python
BOT_ID     = "mybot"          # unique identifier for your bot
CLIENT_ID  = "alice"          # your client's name or ID
PROJECT_ID = "my-project"     # project identifier
SESSIONS_DIR = "/path/to/your/bot/sessions"   # where JSONL session files are stored
DB_PATH    = os.path.expanduser("~/.bot_audit/data/audit.db")  # can stay as-is
```

---

## Step 4: Prepare a session JSONL file

The platform expects session transcripts as JSONL files. Each line is one turn:

**Generic format** (simplest):
```jsonl
{"role": "user", "content": "Can you change the homepage heading to 'Welcome Back'?", "timestamp": "2024-01-01T10:00:00Z"}
{"role": "assistant", "content": "Sure! I'll update the heading text. That's a low-risk UI copy change.", "timestamp": "2024-01-01T10:00:02Z"}
{"role": "user", "content": "Great, go ahead.", "timestamp": "2024-01-01T10:00:10Z"}
{"role": "assistant", "content": "Done. The heading has been updated. Do you want me to deploy to staging?", "timestamp": "2024-01-01T10:00:15Z"}
```

Save this as `~/.bot_audit/sessions/mybot/test-session-001.jsonl`.

---

## Step 5: Process the session

```bash
# From the mybot/ directory:
python3 run_post_session.py --session-file ~/.bot_audit/sessions/mybot/test-session-001.jsonl
```

Output:
```
→ Processing: /Users/you/.bot_audit/sessions/mybot/test-session-001.jsonl
  Session:    test-session-001
  Turns:      4
  Transcript: 412 chars
  Status:  success
  Audit:   3f8a1c2d-...
  Version: 1
```

This will:
1. Format the transcript
2. Call the LLM (OpenRouter) for second-pass extraction
3. Write the `session_audit` event to SQLite
4. Materialize the project state

---

## Step 6: Query the state

```bash
# Show current project state
python3 retrieve.py --status

# Show recent session summaries
python3 retrieve.py --history

# Ask a natural language question
python3 retrieve.py "What did the client ask for?"
python3 retrieve.py "What is pending?"
python3 retrieve.py "What got deployed last?"
```

---

## Step 7: Classify a message (inline)

Use the classifier in your bot's message handler:

```python
import sys
sys.path.insert(0, '/path/to/bot-audit-platform')

from bot_audit.audit import classify_message

classification = classify_message(
    "Can you update the checkout button color to green?",
    use_llm=False  # rules-only for speed
)
print(classification)
# {
#   "intent": "change_request",
#   "action_type": "ui_style_change",
#   "risk_level": "low",
#   "session_tier": "routine",
#   "confidence": 0.9,
#   "method": "rules"
# }
```

---

## Step 8: Manage the SessionManager

```python
from bot_audit.audit import SessionManager

sm = SessionManager(
    bot_id="mybot",
    client_id="alice",
    project_id="my-project",
    db_path="/path/to/audit.db",
)

# On every incoming message:
session = sm.on_message(
    message_id="msg-001",
    message_text="Can you update the homepage?",
    intent_classification=classification,
)

# Close explicitly when done:
closed = sm.close_session(reason="deploy_ack", transcript_path="/path/to/session.jsonl")

# Check idle timeout (call this from a cron or n8n Schedule node):
timed_out = sm.check_idle_timeout()
if timed_out:
    # trigger run_post_session for this closed session
    pass
```

---

## Step 9: Process all sessions (backfill)

```bash
python3 run_post_session.py --all
```

Already-processed sessions are automatically skipped (idempotency via event log).

---

## Step 10: Check the DLQ

```bash
# View status
python3 run_post_session.py --dlq-status

# Retry failed items
python3 run_post_session.py --retry-dlq
```

---

## Phase 1 vs Phase 2 (n8n)

| Mode | When | Behavior |
|------|------|---------|
| **Phase 1 (direct)** | `N8N_AUDIT_WEBHOOK_URL` not set | Pipeline writes directly to SQLite |
| **Phase 2 (n8n)** | Webhook URLs configured | Bot POSTs handoffs to n8n; n8n writes |

To activate n8n mode:
```bash
export N8N_AUDIT_WEBHOOK_URL=http://localhost:5678/webhook/audit
export N8N_EXECUTION_WEBHOOK_URL=http://localhost:5678/webhook/execute
```

See [N8N_SETUP.md](N8N_SETUP.md) for the full n8n configuration.

---

## Running the Test Suite

```bash
cd bot-audit-platform
python3 -c "
import sys, tempfile, os
sys.path.insert(0, '.')
from bot_audit.audit import route, classify_message, get_project_state
from bot_audit.audit.routing import RouteDecision
r = route('logic_change', 'medium')
print(f'Route: {r.worker}, review={r.review}')
c = classify_message('can you fix the login bug')
print(f'Classify: {c[\"intent\"]}, {c[\"action_type\"]}, risk={c[\"risk_level\"]}')
print('All imports OK')
"
```
