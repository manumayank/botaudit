"""
Three-tier execution routing.

v4 Architecture §3 — Routing Table.

n8n uses this routing to decide which worker to invoke for a given change:
  Tier 1: Bash script worker  — simple, deterministic find-and-replace
  Tier 2: Claude Code CLI     — intelligent code changes (no review)
  Tier 3: Claude Code + Codex — intelligent changes requiring independent review

Routing is DETERMINISTIC: action_type + risk_level → worker tier.
n8n makes this decision without any judgment or interpretation.

v4 Routing Table (§3.1):
  ui_copy_change       low      → bash,         no review
  config_change_safe   low      → bash,         no review
  config_change_sensitive high  → claude_code,  codex review
  ui_style_change      low      → claude_code,  no review
  ui_style_change      medium   → claude_code,  codex review
  ui_component_change  medium   → claude_code,  codex review
  logic_change         medium   → claude_code,  codex review
  logic_change         high     → claude_code,  codex review
  data_model_change    high     → claude_code,  codex review
  api_change           high     → claude_code,  codex review
  dependency_change    medium   → claude_code,  codex review
  bug_fix              *        → claude_code,  codex review (always)
  deploy               *        → n8n_direct,   no review
  rollback             critical → n8n_direct,   no review
"""

from typing import NamedTuple


class RouteDecision(NamedTuple):
    worker: str         # bash | claude_code | n8n_direct
    review: bool        # True = Codex review required before deploy
    rationale: str      # Human-readable reason for the route


# Tier 1: Bash — deterministic, low risk
_BASH_ROUTES = {
    ("ui_copy_change", "low"),
    ("config_change_safe", "low"),
}

# Always reviewed regardless of risk
_ALWAYS_REVIEW = {
    "bug_fix",
}

# n8n handles directly (no code change)
_N8N_DIRECT = {
    "deploy",
    "rollback",
}


def route(action_type: str, risk_level: str) -> RouteDecision:
    """
    Determine which worker and whether Codex review is required.

    Returns a RouteDecision. n8n calls this to decide execution path.
    No judgment, no interpretation — pure deterministic lookup.
    """
    key = (action_type, risk_level)

    # Deploy and rollback: n8n handles directly, no code change needed
    if action_type in _N8N_DIRECT:
        return RouteDecision(
            worker="n8n_direct",
            review=False,
            rationale=f"{action_type} is handled directly by n8n, no code generation needed",
        )

    # Bash for simple deterministic changes
    if key in _BASH_ROUTES:
        return RouteDecision(
            worker="bash",
            review=False,
            rationale="Low-risk deterministic change; bash find-and-replace is sufficient",
        )

    # All bug fixes get Claude Code + Codex regardless of risk level
    if action_type in _ALWAYS_REVIEW:
        return RouteDecision(
            worker="claude_code",
            review=True,
            rationale="All bug fixes require independent Codex review regardless of risk level",
        )

    # Medium+ risk: Claude Code + Codex review
    if risk_level in ("medium", "high", "critical"):
        return RouteDecision(
            worker="claude_code",
            review=True,
            rationale=f"risk_level={risk_level} requires Claude Code + Codex review before deploy",
        )

    # Low risk, non-bash: Claude Code without review (e.g. ui_style_change low)
    return RouteDecision(
        worker="claude_code",
        review=False,
        rationale=f"Low-risk change ({action_type}) delegated to Claude Code; no review required",
    )


def requires_approval(risk_level: str) -> bool:
    """
    From v4 §3 and Phase 1 Exec Spec §4.2:
    high → approval required
    critical → approval required + 2nd approver
    """
    return risk_level in ("high", "critical")


def requires_rollback_snapshot(risk_level: str) -> bool:
    """Pre-deploy snapshot required for medium+ risk changes."""
    return risk_level in ("medium", "high", "critical")
