"""
Reference integration config — copy and adapt for your bot.

This is the ONLY file where your bot-specific values live.
The bot_audit.audit platform layer stays generic.
"""

import os
import sys

# Add the repo root to path (adjust if installed as a package)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# --- Bot identity ---
BOT_ID = "mybot"            # Unique bot identifier
CLIENT_ID = "alice"         # Client name or ID
PROJECT_ID = "my-project"  # Project identifier

# --- Deployment target (optional — used in audit summaries) ---
STAGING_URL = "http://your-staging-server"
VPS_HOST = "your-server-ip"
VPS_APP_PATH = "/opt/myapp"

# --- Session files directory ---
# Where your bot stores JSONL session transcripts (one file per session)
SESSIONS_DIR = os.path.expanduser("~/.bot_audit/sessions/mybot")

# --- Database path ---
# All bots can share one DB (separated by bot_id/client_id/project_id)
# or use separate DBs per bot.
DB_PATH = os.path.expanduser("~/.bot_audit/data/audit.db")

# --- n8n webhooks (set when n8n is connected) ---
N8N_AUDIT_WEBHOOK_URL = os.environ.get("N8N_AUDIT_WEBHOOK_URL")
N8N_EXECUTION_WEBHOOK_URL = os.environ.get("N8N_EXECUTION_WEBHOOK_URL")

# --- Session config (override platform defaults if needed) ---
IDLE_TIMEOUT_MINUTES = 15
MAX_DURATION_HOURS = 2
ACK_TIMEOUT_MINUTES = 30
