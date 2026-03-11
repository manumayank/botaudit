"""
Post-session processing pipeline.

v4 Architecture — Phases C & D of the request lifecycle.

OWNERSHIP (v4 §2, Data Access Rule):
  OpenClaw owns: transcript persistence, second-pass extraction, handoff payload delivery
  n8n owns: ALL writes to the audit/state store, client ack flow, workflow triggers

Phase 1 implementation:
  - OpenClaw extraction is implemented here (extractor.py)
  - n8n write path is emulated directly (no n8n running yet)
  - When N8N_AUDIT_WEBHOOK_URL is set, delivery is a POST to n8n
  - When not set, writes happen inline (Phase 1 direct mode)

The function run_post_session() is called by OpenClaw after session close.
It represents the boundary where OpenClaw's work ends and n8n's begins.

Sequence (v4 §4, Phase C):
  1. Session closes → transcript finalized (caller's responsibility)
  2. OpenClaw: second-pass LLM extraction → session_audit payload
  3. OpenClaw: builds audit handoff payload
  4. OpenClaw: POSTs to n8n audit webhook (or direct in Phase 1)
  5. n8n: writes session_audit event to append-only log
  6. n8n: updates project state (optimistic locking)
  7. n8n: triggers downstream workflows
  -> Any step 4-7 failure -> DLQ
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from . import dead_letter as _dlq
from . import events as _events
from . import extractor as _ext
from . import handoff as _handoff
from . import materializer as _mat
from . import config as _cfg


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Primary entry point — called by OpenClaw after session close
# ---------------------------------------------------------------------------

def run_post_session(
    session: Dict[str, Any],
    transcript_text: str,
    transcript_ref: str,
    api_key: Optional[str] = None,
    n8n_audit_webhook_url: Optional[str] = None,
    db_path: str = _cfg.DB_PATH,
) -> Dict[str, Any]:
    """
    Full post-session pipeline for a closed session.

    OpenClaw role:
      - Extract audit record (second pass, separate LLM prompt)
      - Build handoff payload
      - Deliver to n8n (or write directly in Phase 1)

    n8n role (emulated here in Phase 1):
      - Write session_audit event to append-only log
      - Update project state
      - Trigger downstream workflows
    """
    result: Dict[str, Any] = {
        "status": "success",
        "session_audit_event_id": None,
        "project_state_version": None,
        "dlq_id": None,
        "errors": [],
    }

    session_id = session["session_id"]
    bot_id = session["bot_id"]
    client_id = session["client_id"]
    project_id = session["project_id"]
    correlation_id = session["correlation_id"]
    intent_classifications = session.get("intent_classifications", [])

    # OPENCLAW: Second-pass audit extraction
    audit_payload = None
    try:
        audit_payload = _ext.extract_audit_record(
            session_id=session_id,
            transcript_text=transcript_text,
            bot_id=bot_id,
            client_id=client_id,
            project_id=project_id,
            intent_classifications=intent_classifications,
            api_key=api_key,
            transcript_ref=transcript_ref,
        )
    except Exception as e:
        error_msg = f"Audit extraction failed: {e}"
        result["errors"].append(error_msg)
        dlq_id = _dlq.enqueue(
            original_event={
                "session_id": session_id,
                "bot_id": bot_id, "client_id": client_id, "project_id": project_id,
            },
            failure_reason=error_msg,
            failure_stage="audit_write",
            transcript_ref=transcript_ref,
            db_path=db_path,
            alert_severity="error",
            last_error_class=type(e).__name__,
        )
        result["dlq_id"] = dlq_id
        result["status"] = "failed"
        return result

    # OPENCLAW: Build audit handoff payload
    handoff_payload = _handoff.build_audit_handoff(
        bot_id=bot_id,
        client_id=client_id,
        project_id=project_id,
        session_id=session_id,
        correlation_id=correlation_id,
        audit_payload=audit_payload,
        transcript_ref=transcript_ref,
    )

    # Delivery: POST to n8n or write directly (Phase 1)
    webhook_url = n8n_audit_webhook_url or os.environ.get("N8N_AUDIT_WEBHOOK_URL")

    if webhook_url:
        try:
            _handoff.deliver_handoff(handoff_payload, webhook_url=webhook_url)
            result["status"] = "success"
            return result
        except Exception as e:
            result["errors"].append(f"n8n handoff delivery failed: {e}")
            # Fall through to direct write

    # N8N ROLE (Phase 1 emulation): Write audit event + update state
    try:
        event = _events.make_event(
            event_type="session_audit",
            bot_id=bot_id,
            client_id=client_id,
            project_id=project_id,
            actor=bot_id,
            created_by_type="bot",
            payload=audit_payload,
            session_id=session_id,
            correlation_id=correlation_id,
            idempotency_key=f"{bot_id}:{session_id}:session_audit:1",
        )
        _events.append_event(event, db_path=db_path)
        result["session_audit_event_id"] = event["event_id"]
    except Exception as e:
        error_msg = f"Event write failed: {e}"
        result["errors"].append(error_msg)
        dlq_id = _dlq.enqueue(
            original_event={"session_id": session_id, "payload": audit_payload},
            failure_reason=error_msg,
            failure_stage="audit_write",
            transcript_ref=transcript_ref,
            db_path=db_path,
            alert_severity="error",
            last_error_class=type(e).__name__,
        )
        result["dlq_id"] = dlq_id
        result["status"] = "failed"
        return result

    try:
        new_state = _mat.apply_session_audit(
            bot_id=bot_id,
            client_id=client_id,
            project_id=project_id,
            session_audit_event=event,
            db_path=db_path,
        )
        if new_state:
            result["project_state_version"] = new_state.get("status_version")
    except Exception as e:
        error_msg = f"State update failed: {e}"
        result["errors"].append(error_msg)
        dlq_id = _dlq.enqueue(
            original_event=event,
            failure_reason=error_msg,
            failure_stage="state_update",
            transcript_ref=transcript_ref,
            db_path=db_path,
            alert_severity="warning",
            last_error_class=type(e).__name__,
        )
        result["dlq_id"] = dlq_id
        result["status"] = "partial"

    return result


# ---------------------------------------------------------------------------
# Execution handoff — OpenClaw sends to n8n for code changes
# ---------------------------------------------------------------------------

def send_execution_handoff(
    session: Dict[str, Any],
    classification: Dict[str, Any],
    instructions: Dict[str, Any],
    client_confirmed: bool = False,
    n8n_execution_webhook_url: Optional[str] = None,
    db_path: str = _cfg.DB_PATH,
) -> Dict[str, Any]:
    """
    Build and deliver an execution handoff payload to n8n.

    OpenClaw calls this when the client has confirmed a change request
    and it is ready to hand off to n8n for routing + execution.

    Returns the payload dict (also delivered to n8n if webhook configured).
    """
    from . import routing as _routing

    action_type = classification.get("action_type", "informational")
    risk_level = classification.get("risk_level", "none")
    route = _routing.route(action_type, risk_level)

    payload = _handoff.build_execution_handoff(
        bot_id=session["bot_id"],
        client_id=session["client_id"],
        project_id=session["project_id"],
        session_id=session["session_id"],
        correlation_id=session["correlation_id"],
        intent=classification.get("intent", "change_request"),
        action_type=action_type,
        risk_level=risk_level,
        session_tier=classification.get("session_tier", "routine"),
        instructions=instructions,
        client_confirmed=client_confirmed,
        approval_required=_routing.requires_approval(risk_level),
    )

    # Attach routing decision for n8n to consume
    payload["_routing"] = {
        "worker": route.worker,
        "review": route.review,
        "rationale": route.rationale,
        "requires_rollback_snapshot": _routing.requires_rollback_snapshot(risk_level),
    }

    webhook_url = n8n_execution_webhook_url or os.environ.get("N8N_EXECUTION_WEBHOOK_URL")
    if webhook_url:
        try:
            _handoff.deliver_handoff(payload, webhook_url=webhook_url)
        except Exception as e:
            _dlq.enqueue(
                original_event=payload,
                failure_reason=str(e),
                failure_stage="n8n_trigger",
                transcript_ref=f"session:{session['session_id']}",
                db_path=db_path,
                alert_severity="error",
                last_error_class=type(e).__name__,
            )

    return payload


# ---------------------------------------------------------------------------
# DLQ retry
# ---------------------------------------------------------------------------

def retry_dead_letter_item(
    dlq_item: Dict[str, Any],
    api_key: Optional[str] = None,
    n8n_webhook_url: Optional[str] = None,
    db_path: str = _cfg.DB_PATH,
) -> bool:
    """Retry a single DLQ item based on its failure_stage. Returns True on success."""
    dlq_id = dlq_item["dead_letter_id"]
    stage = dlq_item["failure_stage"]
    event = dlq_item["original_event"]

    _dlq.mark_retrying(dlq_id, db_path=db_path)
    try:
        if stage == "audit_write":
            if isinstance(event, dict) and event.get("event_type") == "session_audit":
                _events.append_event(event, db_path=db_path)
        elif stage == "state_update":
            _mat.apply_session_audit(
                bot_id=event["bot_id"],
                client_id=event["client_id"],
                project_id=event["project_id"],
                session_audit_event=event,
                db_path=db_path,
            )
        elif stage == "n8n_trigger" and n8n_webhook_url:
            _handoff.deliver_handoff(event, webhook_url=n8n_webhook_url)

        _dlq.record_retry_result(dlq_id, success=True, db_path=db_path)
        return True
    except Exception as e:
        _dlq.record_retry_result(
            dlq_id, success=False,
            error_reason=str(e), error_class=type(e).__name__,
            db_path=db_path,
        )
        return False
