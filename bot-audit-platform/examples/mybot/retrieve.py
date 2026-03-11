#!/usr/bin/env python3
"""
Policy-based retrieval CLI — reference integration.

Usage:
    python3 retrieve.py "What did the client ask for?"
    python3 retrieve.py "What got deployed last?"
    python3 retrieve.py "What is pending?"
    python3 retrieve.py "What should the client review now?"
    python3 retrieve.py "Was dark mode ever discussed?"
    python3 retrieve.py --status    # Show current project state
    python3 retrieve.py --history   # Show recent session audit summaries
"""

import argparse
import json
import os
import sys

import audit_config as cfg
from bot_audit.audit import retrieve, classify_query, get_project_state, get_events


def query(q: str, verbose: bool = True) -> dict:
    qclass = classify_query(q)
    if verbose:
        print(f"\nQuery class: {qclass}")

    result = retrieve(
        query=q,
        bot_id=cfg.BOT_ID,
        client_id=cfg.CLIENT_ID,
        project_id=cfg.PROJECT_ID,
        db_path=cfg.DB_PATH,
    )

    if verbose:
        print(f"Source:     {result['source']}")
        print(f"Confidence: {result['confidence']:.2f}")
        print(f"Checked:    {' → '.join(result['checked'])}")
        print("\nAnswer:")
        print(json.dumps(result["answer"], indent=2))

    return result


def show_status() -> None:
    """Print current project state."""
    state = get_project_state(cfg.BOT_ID, cfg.CLIENT_ID, cfg.PROJECT_ID, db_path=cfg.DB_PATH)
    if not state:
        print("No project state found. Run run_post_session.py --all first.")
        return

    data = state.get("state_data", {})
    print(f"\n=== Project State (v{state['status_version']}) ===")
    print(f"Status:       {data.get('current_status', state.get('current_status'))}")
    print(f"Last updated: {state['last_updated']}")
    print(f"Last session: {str(state.get('last_session_ref', 'none'))[:8]}...")

    reqs = data.get("active_requests", [])
    print(f"\nActive requests ({len(reqs)}):")
    for r in reqs[:5]:
        print(f"  [{r.get('status','?')}] {r.get('description','')[:80]}")

    review = data.get("awaiting_client_review", [])
    print(f"\nAwaiting client review ({len(review)}):")
    for r in review[:5]:
        print(f"  [{r.get('priority','?')}] {r.get('description','')[:80]}")

    actions = data.get("next_actions", [])
    print(f"\nNext actions ({len(actions)}):")
    for a in actions[:5]:
        print(f"  [{a.get('priority','?')}] {a.get('description','')[:80]}")

    deployed = data.get("deployed_changes", [])
    print(f"\nRecent deploys ({len(deployed)}):")
    for d in deployed[:3]:
        print(f"  [{d.get('status','?')}] {d.get('changes_summary','')[:80]}")


def show_history(limit: int = 5) -> None:
    """Print recent session audit summaries."""
    events = get_events(
        bot_id=cfg.BOT_ID,
        client_id=cfg.CLIENT_ID,
        project_id=cfg.PROJECT_ID,
        event_type="session_audit",
        db_path=cfg.DB_PATH,
    )
    recent = list(reversed(events))[:limit]

    print(f"\n=== Recent Session Audits ({len(events)} total) ===")
    for e in recent:
        p = e.get("payload", {})
        print(f"\n[{e['timestamp'][:19]}] {e['event_id'][:8]} tier={p.get('session_tier','?')}")
        print(f"  {p.get('summary','No summary')[:120]}")
        reqs = p.get("requests_made", [])
        if reqs:
            print(f"  Requests: {', '.join(r.get('description','')[:40] for r in reqs[:3])}")
        pending = p.get("pending_items", [])
        if pending:
            print(f"  Pending:  {', '.join(r.get('description','')[:40] for r in pending[:3])}")


def main():
    parser = argparse.ArgumentParser(description="Bot audit retrieval CLI")
    parser.add_argument("query", nargs="?", help="Query string")
    parser.add_argument("--status", action="store_true", help="Show current project state")
    parser.add_argument("--history", action="store_true", help="Show recent session summaries")
    parser.add_argument("--limit", type=int, default=5, help="History limit (default: 5)")
    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.history:
        show_history(limit=args.limit)
    elif args.query:
        query(args.query)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
