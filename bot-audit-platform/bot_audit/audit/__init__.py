"""
Client-facing bot audit, governance & state platform.

v4 Architecture — Phase 1 implementation, Kapilbook/BookSwap reference.

Four-system roles (v4 §2):
  OpenClaw  → cognition: conversation, classification, extraction, retrieval (READ)
  n8n       → orchestration + persistence: execution routing, event writes, state (WRITE)
  Claude Code → code generation (worker invoked by n8n via claude-worker.sh)
  Codex      → code review (worker invoked by n8n via HTTP Request node)

Public API:
    from bot_audit.audit import SessionManager, retrieve, run_post_session
    from bot_audit.audit import classify_message, get_project_state
    from bot_audit.audit import route, build_execution_handoff
"""

from .events import (
    append_event, get_events, make_event,
    append_review_event, append_human_override, get_review_events,
)
from .dead_letter import enqueue as dlq_enqueue, get_pending as dlq_get_pending, summary as dlq_summary
from .session import SessionManager
from .classifier import classify_message
from .materializer import get_project_state, apply_session_audit, materialize_from_scratch
from .retrieval import retrieve, classify_query
from .pipeline import run_post_session, send_execution_handoff, retry_dead_letter_item
from .routing import route, requires_approval, requires_rollback_snapshot
from .handoff import build_execution_handoff, build_audit_handoff, build_code_change_instructions
from .codex import call_codex_review, is_approved, has_blocking_issues, format_issues_for_retry
from . import config

__all__ = [
    # Core
    "SessionManager",
    "classify_message",
    "retrieve",
    "classify_query",
    "get_project_state",
    "apply_session_audit",
    "materialize_from_scratch",
    # Pipeline
    "run_post_session",
    "send_execution_handoff",
    "retry_dead_letter_item",
    # Events
    "append_event",
    "get_events",
    "make_event",
    "append_review_event",
    "append_human_override",
    "get_review_events",
    # DLQ
    "dlq_enqueue",
    "dlq_get_pending",
    "dlq_summary",
    # Routing (v4)
    "route",
    "requires_approval",
    "requires_rollback_snapshot",
    # Handoff (v4)
    "build_execution_handoff",
    "build_audit_handoff",
    "build_code_change_instructions",
    # Codex review (v4)
    "call_codex_review",
    "is_approved",
    "has_blocking_issues",
    "format_issues_for_retry",
    # Config
    "config",
]
