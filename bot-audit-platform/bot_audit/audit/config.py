"""
Default configuration for the bot audit/governance/state platform.

Override at the call site (db_path, api_key, etc.) or set env vars.
"""

import os

# --- Database ---
# Default location — override per-bot in your audit_config.py
DB_PATH = os.path.expanduser("~/.bot_audit/data/audit.db")

# --- LLM (via OpenRouter, OpenAI-compatible) ---
LLM_API_BASE = "https://openrouter.ai/api/v1"
LLM_API_KEY_ENV = "OPENROUTER_API_KEY"
# Models: prefer claude-3-5-sonnet for extraction quality
LLM_EXTRACTION_MODEL = "anthropic/claude-3.5-sonnet"
LLM_CLASSIFICATION_MODEL = "anthropic/claude-3.5-sonnet"

# Fallback: read API key from a local config file
# Format: {"env": {"OPENROUTER_API_KEY": "sk-..."}}
LOCAL_CONFIG_PATH = os.path.expanduser("~/.bot_audit/config.json")

# --- Session boundaries ---
SESSION_IDLE_TIMEOUT_MINUTES = 15       # close after 15min inactivity
SESSION_MAX_DURATION_HOURS = 2          # hard cap at 2h
SESSION_ACK_TIMEOUT_MINUTES = 30        # deploy+ack wait window
SESSION_CONTINUATION_CONFIDENCE = 0.7  # min confidence to link resumed sessions

# --- Confidence thresholds ---
CONFIDENCE_FLAG_THRESHOLD = 0.7  # below this → flag for human review

# --- Dead letter ---
DLQ_MAX_RETRIES = 3

# --- Session files directory ---
# Directory where your bot stores JSONL session transcripts
SESSIONS_DIR = os.path.expanduser("~/.bot_audit/sessions")
