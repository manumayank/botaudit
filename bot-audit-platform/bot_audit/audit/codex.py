"""
Codex review integration.

v4 Architecture §2.5, §3.4 — The Pathologist.

Codex (OpenAI) independently reviews code changes made by Claude Code (Anthropic).
Different vendor, different model, different blind spots = genuine N-version verification.

n8n invokes Codex via HTTP Request node after Claude Code commits a change.
This module defines:
  - The structured review verdict schema
  - The review prompt constructor (same as n8n uses)
  - A direct Python caller for Phase 1 testing / integration

Codex verdict fields (v4 §2.5):
  approved              bool    Overall: safe to deploy?
  confidence            float   0-1
  scope_match           bool    Changes stayed within target_files?
  spec_match            bool    Changes satisfy change_specification?
  criteria_met          bool    Changes satisfy acceptance_criteria?
  risk_assessment_match bool    Does actual complexity match risk_level?
  risk_escalation       str?    If risk was underestimated, new risk level
  issues                list    [{severity, description, file, line_range}]
  regression_risk       str     none|low|medium|high
  summary               str     One-sentence verdict

n8n routing on verdict (v4 §3.4):
  approved, no issues       → deploy
  approved, warnings        → deploy, log warnings
  rejected, fixable         → route back to Claude Code (max 1 retry)
  rejected, scope_match=F   → no deploy, alert team
  risk_escalation present   → reclassify, re-route
  rejected after retry      → DLQ, alert human
"""

import json
import re
from typing import Any, Dict, List, Optional

from . import llm as _llm
from . import config as _cfg


# ---------------------------------------------------------------------------
# Verdict schema helpers
# ---------------------------------------------------------------------------

def empty_verdict() -> Dict[str, Any]:
    """Neutral verdict template (not approved)."""
    return {
        "approved": False,
        "confidence": 0.0,
        "scope_match": None,
        "spec_match": None,
        "criteria_met": None,
        "risk_assessment_match": None,
        "risk_escalation": None,
        "issues": [],
        "regression_risk": "unknown",
        "summary": "Review not performed",
    }


def is_approved(verdict: Dict[str, Any]) -> bool:
    return bool(verdict.get("approved"))


def has_blocking_issues(verdict: Dict[str, Any]) -> bool:
    """True if any issue has severity=error (blocks deploy)."""
    return any(
        issue.get("severity") == "error"
        for issue in verdict.get("issues", [])
    )


def has_scope_violation(verdict: Dict[str, Any]) -> bool:
    return verdict.get("scope_match") is False


def get_risk_escalation(verdict: Dict[str, Any]) -> Optional[str]:
    return verdict.get("risk_escalation")


def format_issues_for_retry(verdict: Dict[str, Any]) -> str:
    """
    Format Codex issues as a prompt fragment for Claude Code's fix attempt.
    n8n constructs this and prepends to the next Claude Code prompt.
    """
    issues = verdict.get("issues", [])
    if not issues:
        return ""
    lines = ["Codex found the following issues that must be fixed before deploy:"]
    for i in issues:
        severity = i.get("severity", "?").upper()
        desc = i.get("description", "")
        file_ = i.get("file", "")
        line_range = i.get("line_range", "")
        loc = f" in {file_}" if file_ else ""
        loc += f" lines {line_range}" if line_range else ""
        lines.append(f"  [{severity}]{loc}: {desc}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Review prompt construction (v4 §2.5)
# ---------------------------------------------------------------------------

_REVIEW_SYSTEM = (
    "You are a code reviewer. Analyze the diff against the original instructions. "
    "Return ONLY valid JSON matching the verdict schema. "
    "No markdown, no explanation, no preamble. Just JSON."
)

_REVIEW_PROMPT_TEMPLATE = """INSTRUCTIONS: {change_specification}

TARGET FILES: {target_files}

ACCEPTANCE CRITERIA: {acceptance_criteria}

RISK LEVEL: {risk_level}

GIT DIFF:
{git_diff}

Return a JSON object with exactly these fields:
{{
  "approved": <true|false>,
  "confidence": <0.0-1.0>,
  "scope_match": <true|false>,
  "spec_match": <true|false>,
  "criteria_met": <true|false>,
  "risk_assessment_match": <true|false>,
  "risk_escalation": <"low"|"medium"|"high"|"critical"|null>,
  "issues": [
    {{"severity": "<error|warning|info>", "description": "<text>", "file": "<path>", "line_range": "<start-end|null>"}}
  ],
  "regression_risk": "<none|low|medium|high>",
  "summary": "<one sentence>"
}}"""


def build_review_prompt(
    change_specification: str,
    target_files: List[str],
    acceptance_criteria: str,
    risk_level: str,
    git_diff: str,
) -> str:
    return _REVIEW_PROMPT_TEMPLATE.format(
        change_specification=change_specification,
        target_files=", ".join(target_files) if target_files else "not specified",
        acceptance_criteria=acceptance_criteria or "Verify changes match specification",
        risk_level=risk_level,
        git_diff=git_diff[:8000],  # cap diff size
    )


# ---------------------------------------------------------------------------
# Direct Codex caller (for Phase 1 integration testing)
# Phase 2: n8n calls OpenAI API directly via HTTP Request node
# ---------------------------------------------------------------------------

def call_codex_review(
    change_specification: str,
    target_files: List[str],
    acceptance_criteria: str,
    risk_level: str,
    git_diff: str,
    openai_api_key: Optional[str] = None,
    model: str = "codex-mini-latest",
) -> Dict[str, Any]:
    """
    Call Codex (OpenAI) for independent code review.

    Phase 1: Routed through OpenRouter if openai_api_key not provided.
    Phase 2: n8n calls OpenAI API directly; this function is not used.

    Returns structured verdict dict.
    """
    prompt = build_review_prompt(
        change_specification=change_specification,
        target_files=target_files,
        acceptance_criteria=acceptance_criteria,
        risk_level=risk_level,
        git_diff=git_diff,
    )

    # Phase 1: use OpenRouter with a capable model for review
    # (OpenRouter gives access to both Claude and GPT models)
    review_model = f"openai/{model}" if not model.startswith("openai/") else model

    raw = _llm.call_llm(
        prompt=prompt,
        model=review_model,
        api_key=openai_api_key,
        max_tokens=1024,
        temperature=0.1,
        system_prompt=_REVIEW_SYSTEM,
    )

    return _parse_verdict(raw)


def _parse_verdict(raw: str) -> Dict[str, Any]:
    """Parse the LLM verdict response into a clean dict."""
    text = raw.strip()
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        verdict = json.loads(text)
        # Ensure all required fields exist
        base = empty_verdict()
        base.update(verdict)
        return base
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                verdict = json.loads(match.group())
                base = empty_verdict()
                base.update(verdict)
                return base
            except Exception:
                pass
    return empty_verdict()
