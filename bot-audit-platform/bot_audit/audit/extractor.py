"""
Second-pass session audit extractor.

Architecture Decision D3 / Phase 1 Spec Section 2:
- Runs AFTER session close on the raw transcript.
- Uses a SEPARATE LLM prompt (not the conversational bot summarizing itself).
- Avoids self-assessment bias.
- Produces structured session_audit payload with evidence anchors.
- Items below confidence 0.7 are flagged for human review.

Evidence anchors (Section 3):
- decisions_made, actions_taken, requests_made, pending_items
  each include evidence_ref with source_message_ids + source_timestamps.
"""

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import llm as _llm
from . import config as _cfg

_EXTRACTION_SYSTEM_PROMPT = """You are an audit extraction assistant for a client-facing bot system.
Your job is to read a raw conversation transcript and extract a structured audit record.
You are NOT the bot that had this conversation. You are an independent extractor.
Be conservative: if something is unclear, mark confidence low. Never invent facts.
Always link claims to specific message IDs from the transcript."""

_EXTRACTION_PROMPT_TEMPLATE = """Extract a structured audit record from this Telegram conversation transcript.

BOT: {bot_id}
CLIENT: {client_id}
PROJECT: {project_id}
SESSION_ID: {session_id}
INTENT_CLASSIFICATIONS: {intent_classifications_json}

TRANSCRIPT:
{transcript_text}

Respond with ONLY a JSON object (no markdown wrapper). Use this exact structure:

{{
  "summary": "<2-5 sentence human-readable summary of what happened>",
  "intent_classification": ["<intent1>", ...],
  "action_type": "<primary action type from: ui_copy_change|ui_style_change|ui_component_change|logic_change|data_model_change|api_change|config_change|dependency_change|deploy|rollback|bug_fix|informational|feedback_only>",
  "risk_level": "<none|low|medium|high|critical>",
  "session_tier": "<routine|significant|critical>",
  "requests_made": [
    {{
      "request_id": "<uuid>",
      "description": "<what was asked>",
      "status": "<open|in_progress|completed|cancelled>",
      "priority": "<low|medium|high>",
      "evidence_ref": {{
        "source_message_ids": ["<msg_id>", ...],
        "source_timestamps": ["<iso_ts>", ...],
        "extraction_method": "<direct_quote|paraphrase|inferred>",
        "confidence": <0.0-1.0>
      }}
    }}
  ],
  "decisions_made": [
    {{
      "decision_id": "<uuid>",
      "description": "<decision text>",
      "decided_by": "<client|bot|both>",
      "confidence": <0.0-1.0>,
      "evidence_ref": {{
        "source_message_ids": ["<msg_id>", ...],
        "source_timestamps": ["<iso_ts>", ...],
        "extraction_method": "<direct_quote|paraphrase|inferred>",
        "confidence": <0.0-1.0>
      }}
    }}
  ],
  "actions_taken": [
    {{
      "action_id": "<uuid>",
      "type": "<action_type>",
      "description": "<what was done>",
      "result": "<outcome>",
      "files_touched": ["<file_path>", ...],
      "evidence_ref": {{
        "source_message_ids": ["<msg_id>", ...],
        "source_timestamps": ["<iso_ts>", ...],
        "extraction_method": "<direct_quote|paraphrase|inferred>",
        "confidence": <0.0-1.0>
      }}
    }}
  ],
  "pending_items": [
    {{
      "item_id": "<uuid>",
      "description": "<what is pending>",
      "owner": "<client|bot|team>",
      "priority": "<low|medium|high>",
      "evidence_ref": {{
        "source_message_ids": ["<msg_id>", ...],
        "source_timestamps": ["<iso_ts>", ...],
        "extraction_method": "<direct_quote|paraphrase|inferred>",
        "confidence": <0.0-1.0>
      }}
    }}
  ],
  "deploy_actions": [
    {{
      "deploy_id": "<uuid>",
      "target_url": "<url>",
      "changes_summary": "<what changed>",
      "status": "<pending|deployed|verified|rolled_back|failed>",
      "rollback_ref": "<git_hash_or_snapshot_ref>"
    }}
  ],
  "unresolved_items": [
    {{
      "item_id": "<uuid>",
      "description": "<what could not be resolved>",
      "reason": "<why unresolved>"
    }}
  ],
  "client_acknowledgment": "<pending|confirmed|disputed|timeout>",
  "extraction_confidence": {{
    "decisions": <0.0-1.0>,
    "actions": <0.0-1.0>,
    "pending": <0.0-1.0>,
    "overall": <0.0-1.0>
  }},
  "low_confidence_flags": ["<description of any item flagged below 0.7 confidence>"]
}}

IMPORTANT RULES:
1. Every item in requests_made, decisions_made, actions_taken, pending_items MUST have evidence_ref.
2. source_message_ids should reference the "id" field of messages in the transcript.
3. If message IDs are not available, use message timestamps as identifiers.
4. extraction_method should be "direct_quote" only when the client explicitly stated something,
   "paraphrase" when it was clearly implied, "inferred" when you're reading between the lines.
5. If confidence on any item is below 0.7, include it in low_confidence_flags.
6. Return empty arrays [] for sections with nothing to report. Do not omit sections."""


def extract_audit_record(
    session_id: str,
    transcript_text: str,
    bot_id: str,
    client_id: str,
    project_id: str,
    intent_classifications: Optional[List[Dict]] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    transcript_ref: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Run second-pass LLM extraction on a completed session transcript.

    Returns a session_audit payload dict ready to wrap in an event envelope.
    Adds transcript_ref and flags items below confidence threshold.
    """
    prompt = _EXTRACTION_PROMPT_TEMPLATE.format(
        bot_id=bot_id,
        client_id=client_id,
        project_id=project_id,
        session_id=session_id,
        intent_classifications_json=json.dumps(intent_classifications or []),
        transcript_text=transcript_text,
    )

    raw = _llm.call_llm(
        prompt=prompt,
        model=model or _cfg.LLM_EXTRACTION_MODEL,
        api_key=api_key,
        max_tokens=4096,
        temperature=0.1,
        system_prompt=_EXTRACTION_SYSTEM_PROMPT,
    )

    payload = _parse_llm_json(raw)
    payload["transcript_ref"] = transcript_ref or f"session:{session_id}"

    # Flag low-confidence items if not already flagged
    _flag_low_confidence(payload)

    return payload


def _parse_llm_json(raw: str) -> Dict[str, Any]:
    """Parse LLM JSON response, stripping any markdown code fences."""
    text = raw.strip()
    # Strip ```json ... ``` wrappers if present
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # Try to extract first JSON object
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                pass
        raise ValueError(f"Failed to parse LLM response as JSON: {e}\nRaw: {text[:500]}")


def _flag_low_confidence(payload: Dict[str, Any]) -> None:
    """
    Scan all evidence_ref fields. If confidence < 0.7, add to low_confidence_flags.
    """
    threshold = _cfg.CONFIDENCE_FLAG_THRESHOLD
    flags = payload.setdefault("low_confidence_flags", [])

    for field_name in ("decisions_made", "actions_taken", "requests_made", "pending_items"):
        for item in payload.get(field_name, []):
            ev = item.get("evidence_ref", {})
            conf = ev.get("confidence", 1.0)
            if conf < threshold:
                flags.append(
                    f"{field_name}: '{item.get('description', '')}' "
                    f"(confidence={conf:.2f}, method={ev.get('extraction_method', 'unknown')})"
                )

    overall = payload.get("extraction_confidence", {}).get("overall", 1.0)
    if overall < threshold:
        flags.append(f"overall extraction confidence: {overall:.2f}")


def format_transcript_from_jsonl(
    jsonl_lines: List[Dict[str, Any]],
    client_label: str = "CLIENT",
    bot_label: str = "BOT",
) -> str:
    """
    Convert OpenClaw JSONL session records into a readable transcript string.

    Handles two formats:
    1. OpenClaw native: {type: "message", message: {role, content: [...], timestamp}}
    2. Generic:         {role, content, timestamp}

    Strips Telegram metadata wrappers that OpenClaw injects at the top of user messages.
    Each line gets a sequential [N] ID for evidence anchoring.
    """
    lines = []
    msg_id = 1

    for turn in jsonl_lines:
        role = ""
        ts = ""
        content: Any = ""

        # OpenClaw native format: type=message with nested message dict
        if turn.get("type") == "message" and isinstance(turn.get("message"), dict):
            inner = turn["message"]
            role = inner.get("role", "")
            ts = inner.get("timestamp", turn.get("timestamp", ""))
            content = inner.get("content", "")
        else:
            # Generic / legacy format
            role = turn.get("role", turn.get("type", ""))
            ts = turn.get("timestamp", "")
            content = turn.get("content", turn.get("text", ""))

        # Normalize content to plain string
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    block_text = block.get("text", "")
                    if block_text:
                        # Strip Telegram metadata injected by OpenClaw at head of user messages
                        block_text = re.sub(
                            r"Conversation info \(untrusted metadata\):.*?```\s*\n+"
                            r"Sender \(untrusted metadata\):.*?```\s*\n+",
                            "",
                            block_text,
                            flags=re.DOTALL,
                        ).strip()
                        if block_text:
                            parts.append(block_text)
            text = " ".join(p for p in parts if p).strip()
        else:
            text = str(content) if content else ""

        if not text or not role:
            continue

        # Skip non-conversation roles (system context injections)
        if role not in ("user", "assistant", "human", "ai"):
            continue

        label = client_label if role in ("human", "user") else bot_label
        # ts may be int (epoch ms) or ISO string
        if isinstance(ts, (int, float)):
            from datetime import datetime, timezone
            ts_short = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        else:
            ts_short = str(ts)[:19] if ts else ""
        lines.append(f"[{msg_id}] {label} ({ts_short}): {text}")
        msg_id += 1

    return "\n".join(lines)
