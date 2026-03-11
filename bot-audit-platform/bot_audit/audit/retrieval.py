"""
Policy-based retrieval ladder.

Phase 1 Execution Spec, Section 7:
- Classify query as OPERATIONAL or SEMANTIC before retrieving.
- OPERATIONAL ladder: project_state → session_audits → transcripts → semantic → uncertainty
- SEMANTIC ladder: semantic → session_audits → project_state → transcripts → uncertainty
- Each step returns a result with source attribution.
- Never skip steps; always express uncertainty with what was checked.
"""

import json
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from . import events as _events
from . import materializer as _mat
from . import config as _cfg


# ------------------------------------------------------------------
# Query classification
# ------------------------------------------------------------------

_OPERATIONAL_PATTERNS = [
    r"\bpending\b", r"\bactive\b", r"\bstatus\b", r"\bdeployed\b",
    r"\bwhat.*(asked|request)\b", r"\bwhat.*(changed|shipped|done)\b",
    r"\bwhat.*review\b", r"\bwhat.*approve\b", r"\bwhat.*next\b",
    r"\blast.*(deploy|session|change)\b", r"\bcurrent\b", r"\bopen\b",
    r"\bblocker\b", r"\bstaging\b",
]

_SEMANTIC_PATTERNS = [
    r"\bever\b", r"\bdiscussed\b", r"\bmentioned\b", r"\btalk.*about\b",
    r"\bfeel.*about\b", r"\bthink.*about\b", r"\bhistory\b",
    r"\bprevious.*conversation\b", r"\bpast\b", r"\bremember.*when\b",
]


def classify_query(query: str) -> str:
    """Returns 'operational' or 'semantic'."""
    lower = query.lower()
    op_score = sum(1 for p in _OPERATIONAL_PATTERNS if re.search(p, lower))
    sem_score = sum(1 for p in _SEMANTIC_PATTERNS if re.search(p, lower))
    return "operational" if op_score >= sem_score else "semantic"


# ------------------------------------------------------------------
# Retrieval ladder
# ------------------------------------------------------------------

class RetrievalResult:
    def __init__(
        self,
        answer: Any,
        source: str,
        confidence: float,
        strong: bool = False,
        checked: Optional[List[str]] = None,
    ):
        self.answer = answer
        self.source = source
        self.confidence = confidence
        self.strong = strong
        self.checked = checked or []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "answer": self.answer,
            "source": self.source,
            "confidence": self.confidence,
            "strong": self.strong,
            "checked": self.checked,
        }


def retrieve(
    query: str,
    bot_id: str,
    client_id: str,
    project_id: str,
    db_path: str = _cfg.DB_PATH,
    since_days: Optional[int] = 90,
) -> Dict[str, Any]:
    """
    Execute the policy-based retrieval ladder for a query.

    Returns a result dict with answer, source, confidence, and what was checked.
    """
    query_class = classify_query(query)
    checked = []

    if query_class == "operational":
        result = _operational_ladder(
            query, bot_id, client_id, project_id, db_path, since_days, checked
        )
    else:
        result = _semantic_ladder(
            query, bot_id, client_id, project_id, db_path, since_days, checked
        )

    return result.to_dict()


def _operational_ladder(
    query: str,
    bot_id: str,
    client_id: str,
    project_id: str,
    db_path: str,
    since_days: Optional[int],
    checked: List[str],
) -> RetrievalResult:
    """
    Operational query ladder:
    1. Project state (materialized view)
    2. Session audit summaries
    3. Recent completed transcripts (transcript_refs from events)
    4. Semantic memory search (placeholder; skipped in Phase 1 if unavailable)
    5. Uncertainty
    """

    # Step 1: Project state
    checked.append("project_state")
    state = _mat.get_project_state(bot_id, client_id, project_id, db_path)
    if state:
        relevant = _query_project_state(query, state)
        if relevant:
            return RetrievalResult(
                answer=relevant,
                source="project_state",
                confidence=0.95,
                strong=True,
                checked=list(checked),
            )

    # Step 2: Session audit summaries
    checked.append("session_audit_summaries")
    since_ts = _since_timestamp(since_days)
    audit_events = _events.get_events(
        bot_id=bot_id,
        client_id=client_id,
        project_id=project_id,
        event_type="session_audit",
        since=since_ts,
        db_path=db_path,
    )
    if audit_events:
        relevant = _search_audit_summaries(query, audit_events)
        if relevant:
            return RetrievalResult(
                answer=relevant,
                source="session_audit_summaries",
                confidence=0.80,
                strong=True,
                checked=list(checked),
            )

    # Step 3: Transcript references (from audit events)
    checked.append("transcript_refs")
    if audit_events:
        transcript_refs = [
            e["payload"].get("transcript_ref")
            for e in audit_events
            if e["payload"].get("transcript_ref")
        ]
        if transcript_refs:
            return RetrievalResult(
                answer={
                    "message": "No structured audit found; check raw transcripts",
                    "transcript_refs": transcript_refs[-5:],
                },
                source="transcript_refs",
                confidence=0.50,
                strong=False,
                checked=list(checked),
            )

    # Step 4: Semantic memory (not implemented in Phase 1 — note degradation)
    checked.append("semantic_memory")

    # Step 5: Uncertainty
    checked.append("uncertainty")
    return RetrievalResult(
        answer={
            "message": (
                f"No reliable answer found for: '{query}'. "
                f"Checked: {', '.join(checked[:-1])}. "
                "No session audits or project state found for this project."
            )
        },
        source="uncertainty",
        confidence=0.0,
        strong=False,
        checked=list(checked),
    )


def _semantic_ladder(
    query: str,
    bot_id: str,
    client_id: str,
    project_id: str,
    db_path: str,
    since_days: Optional[int],
    checked: List[str],
) -> RetrievalResult:
    """
    Semantic query ladder:
    1. Semantic memory search (Phase 1: basic text search over audit summaries)
    2. Session audit summaries
    3. Project state
    4. Transcript refs
    5. Uncertainty
    """

    since_ts = _since_timestamp(since_days)
    audit_events = _events.get_events(
        bot_id=bot_id,
        client_id=client_id,
        project_id=project_id,
        event_type="session_audit",
        since=since_ts,
        db_path=db_path,
    )

    # Step 1: Text search over audit summaries (Phase 1 semantic proxy)
    checked.append("text_search_audit_summaries")
    if audit_events:
        matches = _text_search_summaries(query, audit_events)
        if matches:
            return RetrievalResult(
                answer=matches,
                source="text_search_audit_summaries",
                confidence=0.65,
                strong=True,
                checked=list(checked),
            )

    # Step 2: Session audit payload search (requests, decisions)
    checked.append("session_audit_payload_search")
    if audit_events:
        deep_matches = _deep_search_audits(query, audit_events)
        if deep_matches:
            return RetrievalResult(
                answer=deep_matches,
                source="session_audit_payload_search",
                confidence=0.60,
                strong=True,
                checked=list(checked),
            )

    # Step 3: Project state
    checked.append("project_state")
    state = _mat.get_project_state(bot_id, client_id, project_id, db_path)
    if state:
        relevant = _query_project_state(query, state)
        if relevant:
            return RetrievalResult(
                answer=relevant,
                source="project_state",
                confidence=0.55,
                strong=False,
                checked=list(checked),
            )

    # Step 4: Transcript refs
    checked.append("transcript_refs")
    if audit_events:
        refs = [e["payload"].get("transcript_ref") for e in audit_events if e["payload"].get("transcript_ref")]
        if refs:
            return RetrievalResult(
                answer={"message": "Topic not found in structured records; check transcripts", "refs": refs[-5:]},
                source="transcript_refs",
                confidence=0.30,
                strong=False,
                checked=list(checked),
            )

    # Step 5: Uncertainty
    checked.append("uncertainty")
    return RetrievalResult(
        answer={"message": f"No records found discussing: '{query}'. Checked: {', '.join(checked[:-1])}."},
        source="uncertainty",
        confidence=0.0,
        strong=False,
        checked=list(checked),
    )


# ------------------------------------------------------------------
# State query helpers
# ------------------------------------------------------------------

def _query_project_state(query: str, state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract relevant slice of project state for the query."""
    lower = query.lower()
    data = state.get("state_data", {})
    result = {}

    if any(w in lower for w in ("pending", "request", "open", "asked")):
        result["active_requests"] = data.get("active_requests", [])
        # "pending" also includes items awaiting review and next actions
        if "pending" in lower:
            result["awaiting_client_review"] = data.get("awaiting_client_review", [])
            result["next_actions"] = data.get("next_actions", [])

    if any(w in lower for w in ("review", "approve", "check", "look at")):
        result["awaiting_client_review"] = data.get("awaiting_client_review", [])

    if any(w in lower for w in ("deployed", "shipped", "changed", "pushed")):
        result["deployed_changes"] = data.get("deployed_changes", [])[:5]

    if any(w in lower for w in ("next", "todo", "action")):
        result["next_actions"] = data.get("next_actions", [])

    if any(w in lower for w in ("blocker", "blocked", "stuck")):
        result["blockers"] = data.get("blockers", [])

    if any(w in lower for w in ("status", "current", "now")):
        result["current_status"] = data.get("current_status")
        result["staging_url"] = data.get("staging_url")

    if not result:
        # Return full state summary if no specific match
        result = {
            "current_status": data.get("current_status"),
            "active_requests_count": len(data.get("active_requests", [])),
            "awaiting_client_review_count": len(data.get("awaiting_client_review", [])),
            "next_actions_count": len(data.get("next_actions", [])),
        }

    return result if any(v for v in result.values() if v) else None


def _search_audit_summaries(query: str, events: List[Dict]) -> Optional[List[Dict]]:
    """Find session audits relevant to the query."""
    lower = query.lower()
    keywords = [w for w in lower.split() if len(w) > 3]
    matches = []

    for event in reversed(events):  # Most recent first
        payload = event.get("payload", {})
        summary = payload.get("summary", "").lower()
        if any(kw in summary for kw in keywords):
            matches.append({
                "event_id": event["event_id"],
                "timestamp": event["timestamp"],
                "summary": payload.get("summary"),
                "session_tier": payload.get("session_tier"),
                "action_type": payload.get("action_type"),
                "pending_items": payload.get("pending_items", [])[:3],
            })
        if len(matches) >= 3:
            break

    return matches if matches else None


def _text_search_summaries(query: str, events: List[Dict]) -> Optional[List[Dict]]:
    """Broader text search for semantic queries."""
    lower = query.lower()
    keywords = [w for w in lower.split() if len(w) > 2]
    matches = []

    for event in reversed(events):
        payload = event.get("payload", {})
        searchable = " ".join([
            payload.get("summary", ""),
            json.dumps(payload.get("requests_made", [])),
            json.dumps(payload.get("decisions_made", [])),
        ]).lower()

        score = sum(1 for kw in keywords if kw in searchable)
        if score > 0:
            matches.append({
                "event_id": event["event_id"],
                "timestamp": event["timestamp"],
                "summary": payload.get("summary"),
                "match_score": score,
            })

    matches.sort(key=lambda x: x["match_score"], reverse=True)
    return matches[:5] if matches else None


def _deep_search_audits(query: str, events: List[Dict]) -> Optional[List[Dict]]:
    """Search inside requests_made and decisions_made for keyword matches."""
    lower = query.lower()
    keywords = [w for w in lower.split() if len(w) > 3]
    matches = []

    for event in reversed(events):
        payload = event.get("payload", {})
        for item in payload.get("requests_made", []) + payload.get("decisions_made", []):
            desc = item.get("description", "").lower()
            if any(kw in desc for kw in keywords):
                matches.append({
                    "event_id": event["event_id"],
                    "timestamp": event["timestamp"],
                    "item": item,
                })

    return matches[:5] if matches else None


def _since_timestamp(since_days: Optional[int]) -> Optional[str]:
    if since_days is None:
        return None
    dt = datetime.now(timezone.utc) - timedelta(days=since_days)
    return dt.isoformat()
