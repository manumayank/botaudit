"""
Handoff payload builder and sender.

v4 Architecture §5 — The Handoff Payload Contract.

OpenClaw generates two handoff payload types:
1. execution — after classifying a change_request: triggers n8n execution workflow
2. audit_record — after session close: triggers n8n audit write + state update

OpenClaw sends these to n8n. n8n does ALL writes to the audit/state store.
OpenClaw reads only.

DATA ACCESS RULE (v4):
  OpenClaw → READS  the audit/state store
  n8n      → WRITES to the audit/state store

For Phase 1 (single machine, n8n not yet connected):
  The n8n write path is emulated by pipeline.py writing directly.
  When n8n is configured, set N8N_EXECUTION_WEBHOOK_URL and N8N_AUDIT_WEBHOOK_URL
  in the environment and this module will POST there instead.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import config as _cfg


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Execution handoff payload (OpenClaw → n8n, after client confirms change)
# ---------------------------------------------------------------------------

def build_execution_handoff(
    bot_id: str,
    client_id: str,
    project_id: str,
    session_id: str,
    correlation_id: str,
    intent: str,
    action_type: str,
    risk_level: str,
    session_tier: str,
    instructions: Dict[str, Any],
    client_confirmed: bool = False,
    approval_required: bool = False,
) -> Dict[str, Any]:
    """
    Build an execution handoff payload (v4 §5.1).

    n8n reads this and routes to: bash script / Claude Code / Claude Code + Codex review.
    The routing decision is deterministic: action_type + risk_level → worker tier.
    """
    idempotency_key = f"{bot_id}:{session_id}:execution:1"
    return {
        "payload_version": 1,
        "handoff_type": "execution",
        "correlation_id": correlation_id,
        "bot_id": bot_id,
        "client_id": client_id,
        "project_id": project_id,
        "session_id": session_id,
        "intent": intent,
        "action_type": action_type,
        "risk_level": risk_level,
        "session_tier": session_tier,
        "client_confirmed": client_confirmed,
        "approval_required": approval_required,
        "instructions": instructions,
        "timestamp": _now_iso(),
        "idempotency_key": idempotency_key,
    }


def build_audit_handoff(
    bot_id: str,
    client_id: str,
    project_id: str,
    session_id: str,
    correlation_id: str,
    audit_payload: Dict[str, Any],
    transcript_ref: str,
) -> Dict[str, Any]:
    """
    Build an audit handoff payload (v4 §5.1).

    OpenClaw sends this after second-pass extraction.
    n8n writes the session_audit event, updates project state, triggers ack flow.
    """
    idempotency_key = f"{bot_id}:{session_id}:audit_record:1"
    return {
        "payload_version": 1,
        "handoff_type": "audit_record",
        "correlation_id": correlation_id,
        "bot_id": bot_id,
        "client_id": client_id,
        "project_id": project_id,
        "session_id": session_id,
        "audit_payload": audit_payload,
        "transcript_ref": transcript_ref,
        "timestamp": _now_iso(),
        "idempotency_key": idempotency_key,
    }


# ---------------------------------------------------------------------------
# Instructions object constructors (v4 §5.2)
# ---------------------------------------------------------------------------

def build_code_change_instructions(
    description: str,
    change_specification: str,
    repo_ref: str,
    target_files: Optional[List[str]] = None,
    acceptance_criteria: Optional[str] = None,
    deploy_after: bool = True,
    commit_message_template: Optional[str] = None,
) -> Dict[str, Any]:
    """Instructions for code change workers (bash script or Claude Code)."""
    return {
        "description": description,
        "change_specification": change_specification,
        "target_files": target_files or [],
        "acceptance_criteria": acceptance_criteria or "",
        "repo_ref": repo_ref,
        "deploy_after": deploy_after,
        "commit_message_template": commit_message_template or "chore: {description} [corr: {corr_id}]",
    }


def build_deploy_instructions(
    target_url: str,
    environment: str,
    commit_ref: str,
    health_check_url: Optional[str] = None,
    health_check_expected: Optional[str] = None,
) -> Dict[str, Any]:
    """Instructions for deploy-only operations (no code change)."""
    return {
        "target_url": target_url,
        "environment": environment,
        "commit_ref": commit_ref,
        "health_check_url": health_check_url or target_url,
        "health_check_expected": health_check_expected or "200",
    }


def build_rollback_instructions(
    rollback_to: str,
    target_url: str,
    reason: str,
    originating_deploy_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Instructions for rollback operations."""
    return {
        "rollback_to": rollback_to,
        "target_url": target_url,
        "reason": reason,
        "originating_deploy_id": originating_deploy_id,
    }


# ---------------------------------------------------------------------------
# Handoff delivery — POST to n8n (or direct call in Phase 1)
# ---------------------------------------------------------------------------

def deliver_handoff(
    payload: Dict[str, Any],
    webhook_url: Optional[str] = None,
) -> bool:
    """
    Send a handoff payload to n8n.

    Returns True on success. Raises on failure (caller should enqueue DLQ).

    Phase 1: If webhook_url is None or empty, returns True (no-op for Phase 1 direct mode).
    Phase 2+: Set N8N_EXECUTION_WEBHOOK_URL / N8N_AUDIT_WEBHOOK_URL environment variables.
    """
    if not webhook_url:
        # Phase 1: direct mode — caller (pipeline.py) handles writes
        return True

    import urllib.request
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15):
        pass
    return True
