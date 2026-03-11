#!/usr/bin/env python3
"""
Post-session audit processor — reference integration.

Usage:
    # Process a specific session JSONL file:
    python3 run_post_session.py --session-file ~/.bot_audit/sessions/mybot/UUID.jsonl

    # Process all unprocessed sessions in SESSIONS_DIR:
    python3 run_post_session.py --all

    # Check dead letter queue health:
    python3 run_post_session.py --dlq-status

    # Retry pending DLQ items:
    python3 run_post_session.py --retry-dlq

Called after each session closes. Can also be run as a cron for backfill.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import audit_config as cfg
from bot_audit.audit import (
    SessionManager,
    run_post_session,
    retry_dead_letter_item,
    dlq_get_pending,
    dlq_summary,
    get_project_state,
    get_events,
)
from bot_audit.audit.extractor import format_transcript_from_jsonl


# ------------------------------------------------------------------
# Session JSONL loader
# ------------------------------------------------------------------

def load_session_jsonl(path: str) -> List[Dict[str, Any]]:
    """Load a session JSONL file into a list of turn dicts."""
    lines = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return lines


def extract_session_metadata(turns: List[Dict]) -> Dict[str, Any]:
    """Extract session metadata from the first record in the JSONL."""
    for turn in turns:
        if turn.get("type") == "session":
            return turn
    return {}


def extract_transcript_turns(turns: List[Dict]) -> List[Dict]:
    """Extract conversation turns (excluding metadata records)."""
    result = []
    for turn in turns:
        if turn.get("type") in ("human", "assistant", "user", "ai", "message"):
            result.append(turn)
        elif turn.get("role") in ("user", "assistant", "human"):
            result.append(turn)
    return result


def get_session_from_db(session_id: str) -> Optional[Dict]:
    """Look up session state from the audit DB."""
    from bot_audit.audit.db import get_db
    conn = get_db(cfg.DB_PATH)
    try:
        row = conn.execute(
            "SELECT * FROM session_state WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        if isinstance(d.get("intent_classifications"), str):
            d["intent_classifications"] = json.loads(d["intent_classifications"])
        return d
    finally:
        conn.close()


def get_processed_session_ids() -> set:
    """Return set of session IDs already in the event log (idempotency check)."""
    events = get_events(
        bot_id=cfg.BOT_ID,
        client_id=cfg.CLIENT_ID,
        project_id=cfg.PROJECT_ID,
        event_type="session_audit",
        db_path=cfg.DB_PATH,
    )
    return {e["session_id"] for e in events if e.get("session_id")}


# ------------------------------------------------------------------
# Core processors
# ------------------------------------------------------------------

def process_session_file(jsonl_path: str, verbose: bool = True) -> Dict[str, Any]:
    """Process a single session JSONL file through the audit pipeline."""
    if verbose:
        print(f"\n→ Processing: {jsonl_path}")

    turns = load_session_jsonl(jsonl_path)
    if not turns:
        print("  Empty file, skipping.")
        return {"status": "skipped", "reason": "empty"}

    meta = extract_session_metadata(turns)
    session_id = meta.get("id") or Path(jsonl_path).stem

    # Idempotency: skip already-processed sessions
    processed = get_processed_session_ids()
    if session_id in processed:
        if verbose:
            print(f"  Already processed (session_id={session_id}), skipping.")
        return {"status": "already_processed", "session_id": session_id}

    conv_turns = extract_transcript_turns(turns)
    if not conv_turns:
        if verbose:
            print(f"  No conversation turns found, skipping.")
        return {"status": "skipped", "reason": "no_turns"}

    # Format transcript for LLM extraction
    transcript_text = format_transcript_from_jsonl(
        conv_turns,
        client_label=cfg.CLIENT_ID.upper(),
        bot_label=cfg.BOT_ID.upper(),
    )

    if verbose:
        print(f"  Session:    {session_id}")
        print(f"  Turns:      {len(conv_turns)}")
        print(f"  Transcript: {len(transcript_text)} chars")

    # Look up session from DB or synthesize minimal record for pre-existing sessions
    session = get_session_from_db(session_id)
    if not session:
        opened_at = meta.get("timestamp") or datetime.now(timezone.utc).isoformat()
        session = {
            "session_id": session_id,
            "bot_id": cfg.BOT_ID,
            "client_id": cfg.CLIENT_ID,
            "project_id": cfg.PROJECT_ID,
            "correlation_id": session_id,
            "status": "closed",
            "opened_at": opened_at,
            "intent_classifications": [],
            "message_count": len(conv_turns),
        }

    # Run the audit pipeline
    result = run_post_session(
        session=session,
        transcript_text=transcript_text,
        transcript_ref=jsonl_path,
        db_path=cfg.DB_PATH,
        n8n_audit_webhook_url=cfg.N8N_AUDIT_WEBHOOK_URL,
    )

    if verbose:
        print(f"  Status:  {result['status']}")
        if result.get("session_audit_event_id"):
            print(f"  Audit:   {result['session_audit_event_id']}")
        if result.get("project_state_version"):
            print(f"  Version: {result['project_state_version']}")
        if result.get("dlq_id"):
            print(f"  DLQ:     {result['dlq_id']}")
        for err in result.get("errors", []):
            print(f"  ERROR:   {err}")

    return result


def process_all_sessions(verbose: bool = True) -> None:
    """Process all unprocessed session JSONL files in SESSIONS_DIR."""
    sessions_dir = Path(cfg.SESSIONS_DIR)
    if not sessions_dir.exists():
        print(f"Sessions directory not found: {sessions_dir}")
        return

    files = sorted(sessions_dir.glob("*.jsonl"))
    if not files:
        print("No session files found.")
        return

    processed = get_processed_session_ids()
    print(f"Found {len(files)} session file(s). {len(processed)} already processed.")

    for path in files:
        session_id = path.stem
        if session_id in processed:
            if verbose:
                print(f"  Skip (done): {path.name}")
            continue
        process_session_file(str(path), verbose=verbose)

    # Print project state summary
    print("\n--- Project State ---")
    state = get_project_state(cfg.BOT_ID, cfg.CLIENT_ID, cfg.PROJECT_ID, db_path=cfg.DB_PATH)
    if state:
        data = state.get("state_data", {})
        print(f"Status:           {data.get('current_status', state.get('current_status'))}")
        print(f"Version:          {state.get('status_version')}")
        print(f"Active requests:  {len(data.get('active_requests', []))}")
        print(f"Awaiting review:  {len(data.get('awaiting_client_review', []))}")
        print(f"Deployed changes: {len(data.get('deployed_changes', []))}")
    else:
        print("No project state yet.")


def show_dlq_status() -> None:
    """Print DLQ health summary."""
    stats = dlq_summary(db_path=cfg.DB_PATH)
    print("\n--- Dead Letter Queue ---")
    for k, v in stats.items():
        if k != "abandoned_items":
            print(f"  {k}: {v}")
    abandoned = stats.get("abandoned_items", [])
    if abandoned:
        print(f"\n  Abandoned (needs attention):")
        for item in abandoned:
            print(f"    [{item['dead_letter_id'][:8]}] {item['failure_stage']}: {item['failure_reason'][:60]}")


def retry_dlq() -> None:
    """Retry all pending DLQ items."""
    pending = dlq_get_pending(db_path=cfg.DB_PATH)
    if not pending:
        print("No pending DLQ items.")
        return
    print(f"Retrying {len(pending)} DLQ item(s)...")
    for item in pending:
        success = retry_dead_letter_item(item, db_path=cfg.DB_PATH)
        status = "OK" if success else "FAIL"
        print(f"  [{status}] [{item['dead_letter_id'][:8]}] {item['failure_stage']}")


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Post-session audit processor")
    parser.add_argument("--session-file", help="Process a specific session JSONL file")
    parser.add_argument("--all", action="store_true", help="Process all unprocessed sessions")
    parser.add_argument("--dlq-status", action="store_true", help="Show DLQ health")
    parser.add_argument("--retry-dlq", action="store_true", help="Retry pending DLQ items")
    parser.add_argument("--quiet", action="store_true", help="Minimal output")
    args = parser.parse_args()

    verbose = not args.quiet

    if args.session_file:
        process_session_file(args.session_file, verbose=verbose)
    elif args.all:
        process_all_sessions(verbose=verbose)
    elif args.dlq_status:
        show_dlq_status()
    elif args.retry_dlq:
        retry_dlq()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
