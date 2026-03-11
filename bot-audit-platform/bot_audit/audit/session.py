"""
Session boundary manager.

Rules (from Phase 1 Execution Spec, Section 1):
- One active session per (bot_id, client_id) pair at a time.
- Session opens on first message after inactivity threshold (15 min default).
- Session closes on: idle timeout, deploy+ack, deploy+timeout (30 min),
  explicit close intent, max duration (2 hours), escalation.
- Every closed session triggers second-pass audit extraction.
- Resumed conversations within idle window extend the current session.
- After idle timeout: new session opened, linked via parent_event_id if
  intent classifier determines continuity >= 0.7 confidence.
"""

import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from . import db as _db
from . import config as _cfg


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: str) -> datetime:
    # Handle both with and without timezone
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        # Fallback for older Python formats
        from datetime import timezone
        dt = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
        return dt.replace(tzinfo=timezone.utc)


class SessionManager:
    """
    Manages session lifecycle for a specific (bot_id, client_id) pair.
    All state persisted in SQLite.
    """

    def __init__(
        self,
        bot_id: str,
        client_id: str,
        project_id: str,
        db_path: str = _cfg.DB_PATH,
        idle_timeout_minutes: int = _cfg.SESSION_IDLE_TIMEOUT_MINUTES,
        max_duration_hours: int = _cfg.SESSION_MAX_DURATION_HOURS,
        ack_timeout_minutes: int = _cfg.SESSION_ACK_TIMEOUT_MINUTES,
    ):
        self.bot_id = bot_id
        self.client_id = client_id
        self.project_id = project_id
        self.db_path = db_path
        self.idle_timeout = timedelta(minutes=idle_timeout_minutes)
        self.max_duration = timedelta(hours=max_duration_hours)
        self.ack_timeout = timedelta(minutes=ack_timeout_minutes)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_message(
        self,
        message_id: str,
        message_text: str,
        timestamp: Optional[str] = None,
        intent_classification: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Process an incoming client message.

        Returns the active session dict (opened or continuing).
        Callers should call check_idle_timeout() periodically (e.g. via n8n cron).
        """
        now_str = timestamp or _now_iso()
        now_dt = _parse_iso(now_str)

        active = self._get_active_session()

        if active:
            # Check max duration cap
            opened_dt = _parse_iso(active["opened_at"])
            if now_dt - opened_dt > self.max_duration:
                self._close_session(active["session_id"], "max_duration", now_str)
                active = None
            else:
                # Extend the session
                self._update_session(active["session_id"], now_str, message_id, intent_classification)
                return active

        if not active:
            # Check if we can link this to the most recent closed session
            parent_id = self._find_continuation_parent()
            session = self._open_session(now_str, parent_id)
            if intent_classification:
                self._update_session(session["session_id"], now_str, message_id, intent_classification)
            return session

        return active

    def get_active_session(self) -> Optional[Dict[str, Any]]:
        """Return the current open session or None."""
        return self._get_active_session()

    def close_session(
        self,
        reason: str,
        transcript_path: Optional[str] = None,
        closed_at: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Explicitly close the active session.

        Returns the closed session dict, or None if no active session.
        """
        active = self._get_active_session()
        if not active:
            return None
        self._close_session(
            active["session_id"],
            reason,
            closed_at or _now_iso(),
            transcript_path,
        )
        return self._get_session_by_id(active["session_id"])

    def check_idle_timeout(self, now: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Check if the active session has exceeded the idle timeout.
        Call this periodically. Returns closed session dict if timed out, else None.
        """
        active = self._get_active_session()
        if not active:
            return None
        now_str = now or _now_iso()
        now_dt = _parse_iso(now_str)
        last_dt = _parse_iso(active["last_message_at"])
        if now_dt - last_dt > self.idle_timeout:
            self._close_session(active["session_id"], "idle_timeout", now_str)
            return self._get_session_by_id(active["session_id"])
        return None

    def check_max_duration(self, now: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Check if the active session has hit the 2-hour max duration cap.
        Returns closed session dict if hit, else None.
        """
        active = self._get_active_session()
        if not active:
            return None
        now_str = now or _now_iso()
        now_dt = _parse_iso(now_str)
        opened_dt = _parse_iso(active["opened_at"])
        if now_dt - opened_dt > self.max_duration:
            self._close_session(active["session_id"], "max_duration", now_str)
            return self._get_session_by_id(active["session_id"])
        return None

    def get_session_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Return the N most recent closed sessions for this bot+client pair."""
        conn = _db.get_db(self.db_path)
        try:
            rows = conn.execute(
                """
                SELECT * FROM session_state
                WHERE bot_id = ? AND client_id = ? AND status != 'open'
                ORDER BY opened_at DESC LIMIT ?
                """,
                (self.bot_id, self.client_id, limit),
            ).fetchall()
            return [_session_row_to_dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_active_session(self) -> Optional[Dict[str, Any]]:
        conn = _db.get_db(self.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM session_state WHERE bot_id = ? AND client_id = ? AND status = 'open'",
                (self.bot_id, self.client_id),
            ).fetchone()
            return _session_row_to_dict(row) if row else None
        finally:
            conn.close()

    def _get_session_by_id(self, session_id: str) -> Optional[Dict[str, Any]]:
        conn = _db.get_db(self.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM session_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return _session_row_to_dict(row) if row else None
        finally:
            conn.close()

    def _open_session(self, opened_at: str, parent_session_id: Optional[str] = None) -> Dict[str, Any]:
        session_id = str(uuid.uuid4())
        correlation_id = str(uuid.uuid4())
        conn = _db.get_db(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO session_state
                    (session_id, bot_id, client_id, project_id, status,
                     correlation_id, opened_at, last_message_at)
                VALUES (?, ?, ?, ?, 'open', ?, ?, ?)
                """,
                (
                    session_id,
                    self.bot_id,
                    self.client_id,
                    self.project_id,
                    correlation_id,
                    opened_at,
                    opened_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        session = self._get_session_by_id(session_id)
        # Store parent_session_id in the dict for callers to use (not persisted separately)
        if parent_session_id:
            session["_parent_session_id"] = parent_session_id
        return session

    def _update_session(
        self,
        session_id: str,
        last_message_at: str,
        message_id: str,
        intent_classification: Optional[Dict],
    ) -> None:
        conn = _db.get_db(self.db_path)
        try:
            # Read current intent_classifications
            row = conn.execute(
                "SELECT intent_classifications, message_count FROM session_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return
            intents = json.loads(row["intent_classifications"] or "[]")
            if intent_classification:
                intents.append({
                    "message_id": message_id,
                    "classification": intent_classification,
                    "timestamp": last_message_at,
                })
            conn.execute(
                """
                UPDATE session_state
                SET last_message_at = ?,
                    message_count = message_count + 1,
                    intent_classifications = ?
                WHERE session_id = ?
                """,
                (last_message_at, json.dumps(intents), session_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _close_session(
        self,
        session_id: str,
        close_reason: str,
        closed_at: str,
        transcript_path: Optional[str] = None,
    ) -> None:
        conn = _db.get_db(self.db_path)
        try:
            conn.execute(
                """
                UPDATE session_state
                SET status = 'closed', close_reason = ?, closed_at = ?, transcript_path = ?
                WHERE session_id = ?
                """,
                (close_reason, closed_at, transcript_path, session_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _find_continuation_parent(self) -> Optional[str]:
        """
        Find the most recent closed session for this bot+client pair,
        as a candidate parent for session linking.
        Actual linking decision (confidence >= 0.7) is left to the extractor.
        """
        conn = _db.get_db(self.db_path)
        try:
            row = conn.execute(
                """
                SELECT session_id FROM session_state
                WHERE bot_id = ? AND client_id = ? AND status = 'closed'
                ORDER BY closed_at DESC LIMIT 1
                """,
                (self.bot_id, self.client_id),
            ).fetchone()
            return row["session_id"] if row else None
        finally:
            conn.close()


def _session_row_to_dict(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    d = dict(row)
    if isinstance(d.get("intent_classifications"), str):
        d["intent_classifications"] = json.loads(d["intent_classifications"])
    return d
