"""
Project state materializer.

Architecture Decision D1 / Phase 1 Spec Section 5.3:
- Project state is a materialized view over the append-only event log.
- Never directly mutate state; always replay or apply incremental updates.
- Optimistic locking: read version, update, write version+1.
  If version changed between read and write, retry with fresh state.
- Answers: "What is going on with this project right now?"

Phase 1 mandatory project state fields:
- project_id, client_id, bot_id
- current_status, status_version, last_updated, last_session_ref
- active_requests, deployed_changes, next_actions
- awaiting_client_review (NEW per Phase 1 spec)
- recent_session_refs
"""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import db as _db
from . import events as _events
from . import config as _cfg


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_project_state(
    bot_id: str,
    client_id: str,
    project_id: str,
    db_path: str = _cfg.DB_PATH,
) -> Optional[Dict[str, Any]]:
    """Return the current project state (materialized view), or None if not yet created."""
    conn = _db.get_db(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM project_state WHERE project_id = ? AND client_id = ? AND bot_id = ?",
            (project_id, client_id, bot_id),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        if isinstance(d.get("state_data"), str):
            d["state_data"] = json.loads(d["state_data"])
        return d
    finally:
        conn.close()


def apply_session_audit(
    bot_id: str,
    client_id: str,
    project_id: str,
    session_audit_event: Dict[str, Any],
    db_path: str = _cfg.DB_PATH,
    max_retries: int = 3,
) -> Dict[str, Any]:
    """
    Apply a session_audit event to the project state.
    Uses optimistic locking. Returns the new state.
    """
    payload = session_audit_event.get("payload", {})
    session_event_id = session_audit_event.get("event_id")

    for attempt in range(max_retries):
        current = get_project_state(bot_id, client_id, project_id, db_path)

        if current is None:
            new_state = _build_initial_state(bot_id, client_id, project_id)
            new_version = 1
        else:
            new_state = dict(current["state_data"])
            new_version = current["status_version"] + 1

        # Apply session audit payload to state
        new_state = _merge_audit_into_state(new_state, payload, session_event_id)
        new_state_json = json.dumps(new_state)

        conn = _db.get_db(db_path)
        try:
            if current is None:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO project_state
                        (project_id, client_id, bot_id, current_status, status_version,
                         last_updated, last_session_ref, state_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id, client_id, bot_id,
                        new_state.get("current_status", "active"),
                        new_version,
                        _now_iso(),
                        session_event_id,
                        new_state_json,
                    ),
                )
                conn.commit()
                return get_project_state(bot_id, client_id, project_id, db_path)
            else:
                # Optimistic locking: only update if version still matches
                cursor = conn.execute(
                    """
                    UPDATE project_state
                    SET current_status = ?,
                        status_version = ?,
                        last_updated = ?,
                        last_session_ref = ?,
                        state_data = ?
                    WHERE project_id = ? AND client_id = ? AND bot_id = ?
                      AND status_version = ?
                    """,
                    (
                        new_state.get("current_status", "active"),
                        new_version,
                        _now_iso(),
                        session_event_id,
                        new_state_json,
                        project_id, client_id, bot_id,
                        current["status_version"],  # must match
                    ),
                )
                conn.commit()
                if cursor.rowcount == 1:
                    return get_project_state(bot_id, client_id, project_id, db_path)
                # Version conflict — retry
        finally:
            conn.close()

    raise RuntimeError(
        f"Optimistic locking failed after {max_retries} retries "
        f"for project {project_id}/{client_id}"
    )


def _build_initial_state(bot_id: str, client_id: str, project_id: str) -> Dict[str, Any]:
    return {
        "project_id": project_id,
        "client_id": client_id,
        "bot_id": bot_id,
        "current_status": "active",
        "active_requests": [],
        "deployed_changes": [],
        "next_actions": [],
        "awaiting_client_review": [],
        "pending_approvals": [],
        "blockers": [],
        "recent_session_refs": [],
        "staging_url": None,
    }


def _merge_audit_into_state(
    state: Dict[str, Any],
    audit_payload: Dict[str, Any],
    session_event_id: Optional[str],
) -> Dict[str, Any]:
    """
    Fold a session_audit payload into the current state dict.
    Conservative merging: add new items, close completed ones, preserve history.
    """
    # Update recent_session_refs (keep last 10)
    refs = state.get("recent_session_refs", [])
    if session_event_id and session_event_id not in refs:
        refs = [session_event_id] + refs[:9]
    state["recent_session_refs"] = refs

    # Merge active_requests
    state["active_requests"] = _merge_requests(
        state.get("active_requests", []),
        audit_payload.get("requests_made", []),
    )

    # Merge deployed_changes from deploy_actions
    for deploy in audit_payload.get("deploy_actions", []):
        entry = {
            "deploy_id": deploy.get("deploy_id"),
            "target_url": deploy.get("target_url"),
            "changes_summary": deploy.get("changes_summary"),
            "status": deploy.get("status"),
            "rollback_ref": deploy.get("rollback_ref"),
            "session_ref": session_event_id,
        }
        deployed = state.get("deployed_changes", [])
        # Keep last 20 deployed changes
        deployed = [entry] + deployed[:19]
        state["deployed_changes"] = deployed

    # Update next_actions from pending_items
    state["next_actions"] = [
        {
            "item_id": item.get("item_id"),
            "description": item.get("description"),
            "owner": item.get("owner"),
            "priority": item.get("priority"),
            "session_ref": session_event_id,
        }
        for item in audit_payload.get("pending_items", [])
        if item.get("owner") == "bot" or item.get("owner") == "team"
    ]

    # awaiting_client_review: items pending client action
    state["awaiting_client_review"] = [
        {
            "item_id": item.get("item_id"),
            "description": item.get("description"),
            "priority": item.get("priority"),
            "session_ref": session_event_id,
        }
        for item in audit_payload.get("pending_items", [])
        if item.get("owner") == "client"
    ]
    # Also flag sessions requiring client acknowledgment
    ack_status = audit_payload.get("client_acknowledgment", "")
    if ack_status == "pending" and session_event_id:
        state["awaiting_client_review"].append({
            "item_id": f"ack:{session_event_id}",
            "description": "Client acknowledgment required for this session",
            "priority": "high",
            "session_ref": session_event_id,
        })

    # Derive current_status from blockers / session tier
    tier = audit_payload.get("session_tier", "routine")
    unresolved = audit_payload.get("unresolved_items", [])
    if unresolved:
        state["current_status"] = "blocked"
    elif state.get("current_status") in ("blocked",) and not unresolved:
        state["current_status"] = "active"

    return state


def _merge_requests(
    existing: List[Dict],
    new_requests: List[Dict],
) -> List[Dict]:
    """Merge new requests; update status of existing ones if found by description similarity."""
    existing_map = {r.get("request_id"): r for r in existing}

    for req in new_requests:
        rid = req.get("request_id")
        if rid in existing_map:
            existing_map[rid].update({"status": req.get("status", existing_map[rid].get("status"))})
        else:
            existing_map[rid] = req

    # Only keep non-completed requests as "active"
    return [r for r in existing_map.values() if r.get("status") not in ("completed", "cancelled")]


def materialize_from_scratch(
    bot_id: str,
    client_id: str,
    project_id: str,
    db_path: str = _cfg.DB_PATH,
) -> Dict[str, Any]:
    """
    Rebuild project state by replaying all session_audit events from the log.
    Use this for recovery or validation.
    """
    all_events = _events.get_events(
        bot_id=bot_id,
        client_id=client_id,
        project_id=project_id,
        event_type="session_audit",
        db_path=db_path,
    )

    # Reset state in DB
    conn = _db.get_db(db_path)
    try:
        conn.execute(
            "DELETE FROM project_state WHERE project_id = ? AND client_id = ? AND bot_id = ?",
            (project_id, client_id, bot_id),
        )
        conn.commit()
    finally:
        conn.close()

    # Replay all session audits in order
    state = None
    for event in all_events:
        state = apply_session_audit(bot_id, client_id, project_id, event, db_path)

    return state or get_project_state(bot_id, client_id, project_id, db_path)
