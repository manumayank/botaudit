"""
Intent + action_type + risk_level classifier.

Phase 1 Execution Spec, Section 4:
- Classify BEFORE the bot acts (inline, not post-hoc).
- intent: informational|change_request|approval|feedback|escalation|bug_report
- action_type: ui_copy_change|ui_style_change|ui_component_change|logic_change|
               data_model_change|api_change|config_change|dependency_change|
               deploy|rollback|bug_fix|informational|feedback_only
- risk_level: none|low|medium|high|critical

Classification strategy:
1. Fast rules-based pass (keyword matching). Handles clear-cut cases.
2. LLM pass for ambiguous messages (optional; can be disabled for speed).
"""

import re
from typing import Dict, Any, Optional, Tuple

from . import llm as _llm
from . import config as _cfg


# ------------------------------------------------------------------
# Intent keywords (rules-based pass)
# ------------------------------------------------------------------

_INTENT_PATTERNS = {
    "escalation": [
        r"\burgent\b", r"\bescalate\b", r"\bmanager\b", r"\bsupport.team\b",
        r"\bhuman\b", r"\bsomeone.else\b",
    ],
    "approval": [
        r"\bapprove\b", r"\bapproved\b", r"\byes.go.ahead\b", r"\bconfirm\b",
        r"\bgo.ahead\b", r"\bproceed\b", r"\bOK\b", r"\bthumbs.up\b",
    ],
    "bug_report": [
        r"\bbug\b", r"\bbroken\b", r"\bnot.working\b", r"\berror\b",
        r"\bcrash\b", r"\bfail\b", r"\bissue\b", r"\bproblem\b",
    ],
    "feedback": [
        r"\blooks good\b", r"\bnice\b", r"\bgreat job\b", r"\bwell done\b",
        r"\bfeedback\b", r"\bthoughts\b", r"\bwhat.do.you.think\b",
    ],
    "change_request": [
        r"\bchange\b", r"\bupdate\b", r"\bmodify\b", r"\badd\b",
        r"\bremove\b", r"\breplace\b", r"\bmake it\b", r"\bcan you\b",
        r"\bplease\b", r"\bI.want\b", r"\bI.need\b",
    ],
}

_ACTION_TYPE_PATTERNS = {
    "deploy": [r"\bdeploy\b", r"\bpush.to\b", r"\bpublish\b", r"\brelease\b"],
    "rollback": [r"\brollback\b", r"\brevert\b", r"\bundo.deploy\b"],
    "data_model_change": [
        r"\bmigration\b", r"\bschema\b", r"\bdatabase\b", r"\bdb\b",
        r"\bcolumn\b", r"\btable\b",
    ],
    "api_change": [
        r"\bAPI\b", r"\bendpoint\b", r"\broute\b", r"\bauth\b",
        r"\btoken\b", r"\bwebhook\b",
    ],
    "config_change": [
        r"\benv\b", r"\bconfig\b", r"\bsettings\b", r"\bfeature.flag\b",
        r"\benvironment.variable\b",
    ],
    "dependency_change": [
        r"\bpackage\b", r"\bnpm\b", r"\bpip\b", r"\bupgrade\b",
        r"\bdependency\b", r"\blibrary\b",
    ],
    "logic_change": [
        r"\bvalidation\b", r"\bflow\b", r"\bbusiness.logic\b",
        r"\bconditional\b", r"\brule\b",
    ],
    "ui_style_change": [
        r"\bcolor\b", r"\bfont\b", r"\bcss\b", r"\bstyle\b",
        r"\bspacing\b", r"\blayout\b",
    ],
    "ui_component_change": [
        r"\bcomponent\b", r"\bpage\b", r"\bsection\b", r"\bbutton\b",
        r"\bform\b", r"\bmodal\b",
    ],
    "ui_copy_change": [
        r"\btext\b", r"\bcopy\b", r"\blabel\b", r"\bheading\b",
        r"\bwording\b", r"\bdescription\b",
    ],
    "bug_fix": [r"\bfix\b", r"\bpatch\b", r"\bresolve.bug\b"],
    "informational": [
        r"\bwhat.is\b", r"\bhow.does\b", r"\bexplain\b",
        r"\bstatus\b", r"\bcheck\b",
    ],
}

_RISK_MAP = {
    "critical": ["data_model_change", "api_change", "deploy", "rollback"],
    "high":     ["config_change", "dependency_change", "logic_change"],
    "medium":   ["ui_component_change", "bug_fix"],
    "low":      ["ui_style_change", "ui_copy_change"],
    "none":     ["informational", "feedback_only"],
}

# Invert for lookup
_ACTION_TO_RISK = {}
for level, actions in _RISK_MAP.items():
    for a in actions:
        _ACTION_TO_RISK[a] = level


def classify_message(
    message_text: str,
    use_llm: bool = True,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Classify a client message.

    Returns:
        {
          "intent": str,           # primary intent
          "action_type": str,      # primary action type
          "risk_level": str,       # none|low|medium|high|critical
          "session_tier": str,     # routine|significant|critical
          "confidence": float,     # 0-1
          "method": str,           # "rules" | "llm"
        }
    """
    intent, action_type, method = _rules_classify(message_text)
    risk_level = _ACTION_TO_RISK.get(action_type, "none")

    if method == "rules" and intent == "informational" and use_llm:
        # Only call LLM when rules are ambiguous (fell back to informational)
        try:
            llm_result = _llm_classify(message_text, api_key=api_key, model=model)
            if llm_result.get("confidence", 0) > 0.6:
                intent = llm_result.get("intent", intent)
                action_type = llm_result.get("action_type", action_type)
                risk_level = llm_result.get("risk_level", risk_level)
                method = "llm"
        except Exception:
            pass  # Stick with rules result

    session_tier = _derive_session_tier(intent, action_type, risk_level)
    confidence = 0.9 if method == "rules" else 0.75

    return {
        "intent": intent,
        "action_type": action_type,
        "risk_level": risk_level,
        "session_tier": session_tier,
        "confidence": confidence,
        "method": method,
    }


def _rules_classify(text: str) -> Tuple[str, str, str]:
    """Fast keyword-based classification. Returns (intent, action_type, method)."""
    lower = text.lower()

    # Intent detection (priority order: escalation > approval > bug_report > feedback > change_request > informational)
    intent = "informational"
    for candidate in ["escalation", "approval", "bug_report", "feedback", "change_request"]:
        patterns = _INTENT_PATTERNS.get(candidate, [])
        if any(re.search(p, lower) for p in patterns):
            intent = candidate
            break

    # Action type detection
    action_type = "informational"
    for candidate, patterns in _ACTION_TYPE_PATTERNS.items():
        if any(re.search(p, lower) for p in patterns):
            action_type = candidate
            break

    # Reconcile intent ↔ action_type
    if intent == "change_request" and action_type == "informational":
        action_type = "ui_copy_change"  # conservative default

    # deploy/rollback action types imply change_request intent
    if action_type in ("deploy", "rollback") and intent == "informational":
        intent = "change_request"

    # bug_fix implies bug_report intent if not already set
    if action_type == "bug_fix" and intent == "informational":
        intent = "bug_report"

    if intent == "feedback":
        action_type = "feedback_only"
    elif intent == "approval" and action_type == "informational":
        # Pure approval message with no specific action → feedback_only
        action_type = "feedback_only"

    return intent, action_type, "rules"


def _llm_classify(
    message_text: str,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Use LLM to classify ambiguous messages.
    Returns partial result dict (intent, action_type, risk_level, confidence).
    """
    prompt = f"""Classify this client message for a bot that manages a web application.

Message: "{message_text}"

Respond with ONLY a JSON object (no markdown, no explanation):
{{
  "intent": "<informational|change_request|approval|feedback|escalation|bug_report>",
  "action_type": "<ui_copy_change|ui_style_change|ui_component_change|logic_change|data_model_change|api_change|config_change|dependency_change|deploy|rollback|bug_fix|informational|feedback_only>",
  "risk_level": "<none|low|medium|high|critical>",
  "confidence": <0.0-1.0>
}}"""

    result = _llm.call_llm(
        prompt=prompt,
        model=model or _cfg.LLM_CLASSIFICATION_MODEL,
        api_key=api_key,
        max_tokens=150,
        temperature=0.1,
    )
    import json
    try:
        return json.loads(result.strip())
    except Exception:
        return {}


def _derive_session_tier(intent: str, action_type: str, risk_level: str) -> str:
    """
    Session tiering (Phase 1 Execution Spec, Section 2.2):
    - critical: deploy to production, rollback, escalation, high-risk approval
    - significant: deploy to staging, change_request w/ risk>=medium, approvals, bug_reports
    - routine: everything else
    """
    if (
        action_type in ("deploy", "rollback")
        or intent == "escalation"
        or risk_level == "critical"
    ):
        return "critical"
    if (
        intent in ("approval", "bug_report")
        or (intent == "change_request" and risk_level in ("medium", "high"))
    ):
        return "significant"
    return "routine"
