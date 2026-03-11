"""
Append-only event log.

Rules:
- Events are NEVER updated or deleted after writing.
- Idempotency key prevents duplicates on retry.
- All event types share the same envelope schema.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import db as _db
from . import config as _cfg


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_event(
    event_type: str,
    bot_id: str,
    client_id: str,
    project_id: str,
    actor: str,
    created_by_type: str,       # client | bot | system | operator
    payload: Dict[str, Any],
    session_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    causation_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    parent_event_id: Optional[str] = None,
    confidence: Optional[float] = None,
    event_id: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build a validated event envelope dict.
    Call append_event() to persist it.
    """
    eid = event_id or str(uuid.uuid4())
    ikey = idempotency_key or f"{bot_id}:{session_id or 'nosession'}:{event_type}:{eid}"
    return {
        "event_id": eid,
        "event_type": event_type,
        "timestamp": timestamp or _now_iso(),
        "bot_id": bot_id,
        "client_id": client_id,
        "project_id": project_id,
        "session_id": session_id,
        "actor": actor,
        "schema_version": 1,
        "correlation_id": correlation_id,
        "causation_id": causation_id,
        "idempotency_key": ikey,
        "created_by_type": created_by_type,
        "payload": payload,
        "confidence": confidence,
        "parent_event_id": parent_event_id,
    }


def append_event(event: Dict[str, Any], db_path: str = _cfg.DB_PATH) -> bool:
    """
    Write event to the append-only log.

    Returns True if written, False if idempotency key already exists (safe no-op).
    Raises on any other error.
    """
    conn = _db.get_db(db_path)
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO events
                (event_id, event_type, timestamp, bot_id, client_id, project_id,
                 session_id, actor, schema_version, correlation_id, causation_id,
                 idempotency_key, created_by_type, payload, confidence, parent_event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["event_id"],
                event["event_type"],
                event["timestamp"],
                event["bot_id"],
                event["client_id"],
                event["project_id"],
                event.get("session_id"),
                event["actor"],
                event.get("schema_version", 1),
                event.get("correlation_id"),
                event.get("causation_id"),
                event["idempotency_key"],
                event["created_by_type"],
                json.dumps(event["payload"]),
                event.get("confidence"),
                event.get("parent_event_id"),
            ),
        )
        conn.commit()
        # INSERT OR IGNORE: check if we actually wrote
        cursor = conn.execute(
            "SELECT id FROM events WHERE idempotency_key = ?",
            (event["idempotency_key"],)
        )
        row = cursor.fetchone()
        return row is not None
    finally:
        conn.close()


def get_events(
    bot_id: str,
    client_id: str,
    project_id: str,
    event_type: Optional[str] = None,
    since: Optional[str] = None,
    session_id: Optional[str] = None,
    db_path: str = _cfg.DB_PATH,
) -> List[Dict[str, Any]]:
    """
    Query events for a bot+client+project.
    Returns list of dicts with payload already deserialized.
    """
    conn = _db.get_db(db_path)
    try:
        clauses = ["bot_id = ?", "client_id = ?", "project_id = ?"]
        params: List[Any] = [bot_id, client_id, project_id]

        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if since:
            clauses.append("timestamp >= ?")
            params.append(since)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)

        sql = (
            "SELECT * FROM events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY timestamp ASC"
        )
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_event_by_id(event_id: str, db_path: str = _cfg.DB_PATH) -> Optional[Dict[str, Any]]:
    conn = _db.get_db(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def get_events_by_correlation(
    correlation_id: str,
    db_path: str = _cfg.DB_PATH
) -> List[Dict[str, Any]]:
    """Return all events from one session lifecycle."""
    conn = _db.get_db(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM events WHERE correlation_id = ? ORDER BY timestamp ASC",
            (correlation_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def append_review_event(
    bot_id: str,
    client_id: str,
    project_id: str,
    session_id: str,
    correlation_id: str,
    reviewer: str,
    reviewer_model: str,
    verdict: Dict[str, Any],
    originating_action_id: Optional[str],
    retry_number: int = 0,
    db_path: str = _cfg.DB_PATH,
) -> Dict[str, Any]:
    """
    Write a review_event to the append-only log AND to the review_events denorm table.

    Called by n8n after Codex (or any reviewer) produces a verdict.
    Returns the event dict.
    """
    event = make_event(
        event_type="review_event",
        bot_id=bot_id,
        client_id=client_id,
        project_id=project_id,
        actor=reviewer,
        created_by_type="system",
        session_id=session_id,
        correlation_id=correlation_id,
        causation_id=originating_action_id,
        idempotency_key=f"{bot_id}:{session_id}:review_event:{retry_number}",
        payload={
            "reviewer": reviewer,
            "reviewer_model": reviewer_model,
            "verdict": verdict,
            "originating_action_id": originating_action_id,
            "retry_number": retry_number,
        },
        confidence=verdict.get("confidence"),
    )
    append_event(event, db_path=db_path)

    # Also write to denormalized review_events table for fast query
    conn = _db.get_db(db_path)
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO review_events
                (review_event_id, correlation_id, bot_id, client_id, project_id,
                 reviewer, reviewer_model, originating_action_id,
                 approved, confidence, scope_match, spec_match,
                 criteria_met, risk_assessment_match, risk_escalation,
                 regression_risk, verdict_data, retry_number, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["event_id"],
                correlation_id,
                bot_id, client_id, project_id,
                reviewer, reviewer_model,
                originating_action_id,
                1 if verdict.get("approved") else 0,
                verdict.get("confidence"),
                1 if verdict.get("scope_match") else 0,
                1 if verdict.get("spec_match") else 0,
                1 if verdict.get("criteria_met") else 0,
                1 if verdict.get("risk_assessment_match") else 0,
                verdict.get("risk_escalation"),
                verdict.get("regression_risk"),
                json.dumps(verdict),
                retry_number,
                event["timestamp"],
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return event


def append_human_override(
    bot_id: str,
    client_id: str,
    project_id: str,
    session_id: str,
    correlation_id: str,
    operator: str,
    override_point: str,
    scope: str,
    original_decision: Dict[str, Any],
    human_decision: Dict[str, Any],
    reason: str,
    affected_event_ids: Optional[List[str]] = None,
    db_path: str = _cfg.DB_PATH,
) -> Dict[str, Any]:
    """
    Write a human_override event. Called any time a human overrides an AI decision.

    override_point: pre_execution | post_code_change | post_review | post_deploy | pause | resume
    scope: global | project | bot | single_event
    reason: MANDATORY — cannot be empty per v4 §11.2
    """
    if not reason or not reason.strip():
        raise ValueError("reason is mandatory for human_override events")

    event = make_event(
        event_type="human_override",
        bot_id=bot_id,
        client_id=client_id,
        project_id=project_id,
        actor=operator,
        created_by_type="operator",
        session_id=session_id,
        correlation_id=correlation_id,
        idempotency_key=f"{bot_id}:{session_id}:human_override:{_now_iso()}",
        payload={
            "operator": operator,
            "override_point": override_point,
            "scope": scope,
            "original_decision": original_decision,
            "human_decision": human_decision,
            "reason": reason,
            "affected_event_ids": affected_event_ids or [],
        },
    )
    append_event(event, db_path=db_path)
    return event


def get_review_events(
    bot_id: str,
    client_id: str,
    project_id: str,
    correlation_id: Optional[str] = None,
    db_path: str = _cfg.DB_PATH,
) -> List[Dict[str, Any]]:
    """Query the denormalized review_events table. Used by OpenClaw retrieval."""
    conn = _db.get_db(db_path)
    try:
        clauses = ["bot_id = ?", "client_id = ?", "project_id = ?"]
        params: List[Any] = [bot_id, client_id, project_id]
        if correlation_id:
            clauses.append("correlation_id = ?")
            params.append(correlation_id)
        rows = conn.execute(
            "SELECT * FROM review_events WHERE " + " AND ".join(clauses)
            + " ORDER BY timestamp DESC",
            params,
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("verdict_data"), str):
                d["verdict_data"] = json.loads(d["verdict_data"])
            result.append(d)
        return result
    finally:
        conn.close()


def _row_to_dict(row: Any) -> Dict[str, Any]:
    d = dict(row)
    if isinstance(d.get("payload"), str):
        d["payload"] = json.loads(d["payload"])
    return d
