"""
Dead letter queue (DLQ) for failed post-session processing.

Architecture Decision D8 / Phase 1 Spec Section 8.3:
- If ANY post-session step fails (audit write, state update, n8n trigger),
  the raw transcript + failure marker are preserved here.
- Never silently discard failures. DLQ over silent gaps. Always.
- Recovery process monitors and retries.
- Alerts on persistent failures.

failure_stage enum: audit_write | state_update | n8n_trigger | deploy | notification
"""

import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from . import db as _db
from . import config as _cfg


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def enqueue(
    original_event: Dict[str, Any],
    failure_reason: str,
    failure_stage: str,
    transcript_ref: str,
    db_path: str = _cfg.DB_PATH,
    max_retries: int = _cfg.DLQ_MAX_RETRIES,
    alert_severity: str = "warning",
    last_error_class: Optional[str] = None,
    owner: Optional[str] = None,
) -> str:
    """
    Park a failed event in the dead letter queue.

    Returns the dead_letter_id.
    Raw transcript is ALWAYS preserved regardless of the failure.
    """
    dlq_id = str(uuid.uuid4())
    next_retry = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()

    conn = _db.get_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO dead_letter
                (dead_letter_id, original_event, failure_reason, failure_stage,
                 retry_count, max_retries, next_retry_at, status, transcript_ref,
                 owner, alert_severity, last_error_class, escalated)
            VALUES (?, ?, ?, ?, 0, ?, ?, 'pending', ?, ?, ?, ?, 0)
            """,
            (
                dlq_id,
                json.dumps(original_event),
                failure_reason,
                failure_stage,
                max_retries,
                next_retry,
                transcript_ref,
                owner,
                alert_severity,
                last_error_class,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return dlq_id


def get_pending(
    db_path: str = _cfg.DB_PATH,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """
    Return DLQ items that are due for retry (status=pending or retrying, next_retry_at <= now).
    """
    now = _now_iso()
    conn = _db.get_db(db_path)
    try:
        rows = conn.execute(
            """
            SELECT * FROM dead_letter
            WHERE status IN ('pending', 'retrying')
              AND retry_count < max_retries
              AND (next_retry_at IS NULL OR next_retry_at <= ?)
            ORDER BY created_at ASC LIMIT ?
            """,
            (now, limit),
        ).fetchall()
        return [_dlq_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_abandoned(db_path: str = _cfg.DB_PATH) -> List[Dict[str, Any]]:
    """Return items that have exhausted retries."""
    conn = _db.get_db(db_path)
    try:
        rows = conn.execute(
            """
            SELECT * FROM dead_letter
            WHERE status = 'abandoned'
            ORDER BY created_at DESC
            """,
        ).fetchall()
        return [_dlq_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def mark_retrying(
    dead_letter_id: str,
    db_path: str = _cfg.DB_PATH,
) -> None:
    """Mark a DLQ item as being retried now."""
    conn = _db.get_db(db_path)
    try:
        conn.execute(
            "UPDATE dead_letter SET status = 'retrying' WHERE dead_letter_id = ?",
            (dead_letter_id,),
        )
        conn.commit()
    finally:
        conn.close()


def record_retry_result(
    dead_letter_id: str,
    success: bool,
    error_reason: Optional[str] = None,
    error_class: Optional[str] = None,
    db_path: str = _cfg.DB_PATH,
) -> str:
    """
    Update a DLQ item after a retry attempt.
    Returns new status: 'resolved' | 'pending' | 'abandoned'.
    """
    conn = _db.get_db(db_path)
    try:
        row = conn.execute(
            "SELECT retry_count, max_retries FROM dead_letter WHERE dead_letter_id = ?",
            (dead_letter_id,),
        ).fetchone()
        if not row:
            return "not_found"

        new_count = row["retry_count"] + 1

        if success:
            conn.execute(
                "UPDATE dead_letter SET status = 'resolved', retry_count = ? WHERE dead_letter_id = ?",
                (new_count, dead_letter_id),
            )
            conn.commit()
            return "resolved"

        if new_count >= row["max_retries"]:
            conn.execute(
                """
                UPDATE dead_letter
                SET status = 'abandoned', retry_count = ?, last_error_class = ?, escalated = 1
                WHERE dead_letter_id = ?
                """,
                (new_count, error_class, dead_letter_id),
            )
            conn.commit()
            return "abandoned"

        # Exponential backoff: 5min * 2^retry_count
        backoff_minutes = 5 * (2 ** new_count)
        next_retry = (
            datetime.now(timezone.utc) + timedelta(minutes=backoff_minutes)
        ).isoformat()

        conn.execute(
            """
            UPDATE dead_letter
            SET status = 'pending', retry_count = ?, next_retry_at = ?,
                failure_reason = ?, last_error_class = ?
            WHERE dead_letter_id = ?
            """,
            (new_count, next_retry, error_reason or "", error_class, dead_letter_id),
        )
        conn.commit()
        return "pending"
    finally:
        conn.close()


def resolve(
    dead_letter_id: str,
    db_path: str = _cfg.DB_PATH,
) -> None:
    """Manually mark a DLQ item as resolved."""
    conn = _db.get_db(db_path)
    try:
        conn.execute(
            "UPDATE dead_letter SET status = 'resolved' WHERE dead_letter_id = ?",
            (dead_letter_id,),
        )
        conn.commit()
    finally:
        conn.close()


def summary(db_path: str = _cfg.DB_PATH) -> Dict[str, Any]:
    """Return DLQ health summary for monitoring."""
    conn = _db.get_db(db_path)
    try:
        stats = {}
        for status in ("pending", "retrying", "resolved", "abandoned"):
            row = conn.execute(
                "SELECT COUNT(*) as n FROM dead_letter WHERE status = ?", (status,)
            ).fetchone()
            stats[status] = row["n"] if row else 0

        abandoned_rows = conn.execute(
            "SELECT * FROM dead_letter WHERE status = 'abandoned' ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
        stats["abandoned_items"] = [_dlq_row_to_dict(r) for r in abandoned_rows]

        return stats
    finally:
        conn.close()


def _dlq_row_to_dict(row: Any) -> Dict[str, Any]:
    d = dict(row)
    if isinstance(d.get("original_event"), str):
        try:
            d["original_event"] = json.loads(d["original_event"])
        except Exception:
            pass
    return d
