# Architecture

## Overview: Four-System Model (v4)

The platform is built around a strict separation of concerns between four systems:

| System | Role | Data Access |
|--------|------|-------------|
| **Your Bot / OpenClaw** | Conversation, classification, extraction, retrieval | READ only |
| **n8n** | Orchestration, event writes, state updates, workflow triggers | WRITE only |
| **Claude Code** | Code generation (invoked by n8n via `claude-worker.sh`) | — |
| **Codex (OpenAI)** | Code review (invoked by n8n via HTTP Request node) | — |

**The data access rule is a hard boundary:**
- The bot reads the audit/state store to answer client questions
- n8n writes to the audit/state store after receiving handoff payloads from the bot
- The bot never writes directly (except in Phase 1 direct mode for development)

---

## Request Lifecycle

```
Phase A: Conversation
  Client message → Bot classifies (intent + action_type + risk_level)
  Bot answers → Session state updated in-flight

Phase B: Pre-execution
  Client confirms change request
  Bot builds execution handoff payload → POSTs to n8n
  n8n checks routing table → selects worker tier

Phase C: Execution
  Worker executes change
  Claude Code commits → Codex reviews diff
  If approved → deploy
  If rejected → retry once → DLQ if still failing

Phase D: Post-session
  Session closes (idle timeout / explicit close / max duration / deploy+ack)
  Bot runs second-pass LLM extraction on transcript
  Bot builds audit handoff payload → POSTs to n8n
  n8n writes session_audit event → updates project state
```

---

## Session Boundary Rules

A session opens on the first message after the idle window and closes when:

| Trigger | Default |
|---------|---------|
| Idle timeout | 15 minutes |
| Max duration | 2 hours |
| Deploy + client acknowledgment | Immediate |
| Deploy + ack timeout | 30 minutes |
| Explicit close intent | Immediate |
| Escalation event | Immediate |

One active session per `(bot_id, client_id)` pair at a time, enforced by a unique partial index on `session_state` where `status='open'`.

---

## Event Schema

Every event shares this envelope:

```json
{
  "event_id":        "uuid",
  "event_type":      "session_audit | review_event | human_override | ...",
  "timestamp":       "2024-01-01T10:00:00+00:00",
  "bot_id":          "mybot",
  "client_id":       "alice",
  "project_id":      "my-project",
  "session_id":      "uuid",
  "actor":           "mybot",
  "schema_version":  1,
  "correlation_id":  "uuid",
  "causation_id":    "uuid | null",
  "idempotency_key": "mybot:session-uuid:event_type:1",
  "created_by_type": "bot | client | system | operator",
  "payload":         { ... },
  "confidence":      0.92
}
```

Events are **never updated or deleted** after writing. Idempotency key prevents duplicates on retry.

---

## Session Audit Payload (second-pass extraction)

The LLM extractor produces this payload after each session closes:

```json
{
  "summary": "Client asked to fix search ranking and deploy to staging",
  "intent_classification": ["change_request"],
  "action_type": "logic_change",
  "risk_level": "medium",
  "session_tier": "significant",
  "requests_made": [{
    "request_id": "uuid",
    "description": "Fix the Haversine distance scoring in SwapFeed",
    "status": "in_progress",
    "priority": "high",
    "evidence_ref": {
      "source_message_ids": ["msg-3", "msg-7"],
      "source_timestamps": ["2024-01-01T10:02:00Z"],
      "extraction_method": "direct_quote",
      "confidence": 0.95
    }
  }],
  "decisions_made": [...],
  "actions_taken": [...],
  "pending_items": [{
    "item_id": "uuid",
    "description": "Deploy fix to staging after Codex review",
    "owner": "bot",
    "priority": "high",
    "evidence_ref": { ... }
  }],
  "deploy_actions": [...],
  "client_acknowledgment": "pending",
  "extraction_confidence": {
    "decisions": 0.92,
    "actions": 0.88,
    "pending": 0.90,
    "overall": 0.90
  },
  "low_confidence_flags": []
}
```

Evidence anchors are mandatory on every extracted item. Items below 0.7 confidence are flagged in `low_confidence_flags` for human review.

---

## Project State Materialization

Project state is a **materialized view** rebuilt by replaying `session_audit` events. It is never directly mutated — always derived from the event log.

```json
{
  "current_status": "active",
  "status_version": 12,
  "active_requests": [...],
  "awaiting_client_review": [...],
  "deployed_changes": [...],
  "next_actions": [...],
  "blockers": [],
  "staging_url": "https://staging.example.com"
}
```

**Optimistic locking:** reads `status_version`, writes `version+1`. If another process wrote between read and write, retry up to 3 times. This prevents concurrent session races.

**`awaiting_client_review`** is populated from two sources:
1. `pending_items` where `owner == "client"`
2. Sessions where `client_acknowledgment == "pending"`

---

## Three-Tier Execution Routing

Routing is **deterministic**: `action_type + risk_level` maps to a worker. n8n makes no judgment calls.

| action_type | risk_level | worker | codex_review |
|-------------|-----------|--------|-------------|
| `ui_copy_change` | low | bash | no |
| `config_change_safe` | low | bash | no |
| `ui_style_change` | low | claude_code | no |
| `ui_style_change` | medium | claude_code | **yes** |
| `ui_component_change` | medium | claude_code | **yes** |
| `logic_change` | medium | claude_code | **yes** |
| `logic_change` | high | claude_code | **yes** |
| `data_model_change` | high | claude_code | **yes** |
| `api_change` | high | claude_code | **yes** |
| `dependency_change` | medium | claude_code | **yes** |
| `bug_fix` | any | claude_code | **yes** (always) |
| `deploy` | any | n8n_direct | no |
| `rollback` | any | n8n_direct | no |

**Approval required:** `risk_level` in `(high, critical)` → human must approve before n8n executes.

**Rollback snapshot required:** `risk_level` in `(medium, high, critical)` → n8n takes a git snapshot before executing.

---

## N-Version Verification (Claude Code + Codex)

For Tier 3 changes:

1. **n8n** passes a prompt + instructions to `claude-worker.sh`
2. **Claude Code** (Anthropic) generates the code change and commits
3. **n8n** reads the git diff and calls **Codex** (OpenAI) with the diff + original spec
4. **Codex** returns a structured verdict:

```json
{
  "approved": true,
  "confidence": 0.94,
  "scope_match": true,
  "spec_match": true,
  "criteria_met": true,
  "risk_assessment_match": true,
  "risk_escalation": null,
  "issues": [],
  "regression_risk": "low",
  "summary": "Change matches spec. No issues found."
}
```

Different vendor, different model, different blind spots = genuine N-version verification.

**n8n routing on verdict:**
- `approved, no issues` → deploy
- `approved, warnings` → deploy, log warnings
- `rejected, fixable` → retry once (Claude Code re-attempts with issue list)
- `rejected, scope_match=false` → no deploy, alert team
- `risk_escalation present` → reclassify and re-route
- `rejected after retry` → DLQ, alert human

---

## claude-worker.sh: Trust Git, Not Claude

The wrapper derives all output from **git state**, not from Claude's self-reported output.

```
Inputs:  project_dir, prompt_file, output_file
Outputs: {success, pre_hash, commit_hash, files_changed, diff_stat}
```

Validation steps:
1. Clean working tree before starting (refuses dirty repos)
2. Claude Code exit code check
3. Actual new commit present (`post_hash != pre_hash`)
4. `files_changed` derived from `git diff --name-only`, not Claude's claims

This is critical: Claude may say "I changed 2 files" when it changed 3, or vice versa.

---

## Handoff Payload Contract

OpenClaw sends two types of handoff payloads to n8n:

### Execution Handoff (change request confirmed by client)
```json
{
  "payload_version": 1,
  "handoff_type": "execution",
  "correlation_id": "uuid",
  "bot_id": "mybot",
  "action_type": "logic_change",
  "risk_level": "medium",
  "client_confirmed": true,
  "approval_required": false,
  "instructions": {
    "description": "Fix search ranking",
    "change_specification": "Update Haversine scoring",
    "target_files": ["app/search.py"],
    "acceptance_criteria": "All tests pass"
  },
  "_routing": {
    "worker": "claude_code",
    "review": true,
    "requires_rollback_snapshot": true
  }
}
```

### Audit Handoff (after session close + extraction)
```json
{
  "payload_version": 1,
  "handoff_type": "audit_record",
  "correlation_id": "uuid",
  "session_id": "uuid",
  "audit_payload": { ... },
  "transcript_ref": "/path/to/session.jsonl"
}
```

---

## Dead Letter Queue

Failed post-session processing is enqueued in the DLQ rather than lost. The raw transcript reference is always preserved.

**Failure stages:**
- `audit_write` — event log write failed
- `state_update` — project state update failed
- `n8n_trigger` — handoff delivery to n8n failed

**Retry schedule:**
- Retry 1: now + 5 min
- Retry 2: now + 10 min
- Retry 3: now + 20 min
- After max_retries: status = `abandoned`, `escalated = 1` (alert required)

**Recovery:** `python3 run_post_session.py --retry-dlq`

---

## SQLite Design Notes

- WAL mode for concurrent reads without blocking writes
- Unique partial index on `session_state(bot_id, client_id) WHERE status='open'` enforces one active session per pair at the DB level
- `INSERT OR IGNORE` on events (idempotency) — retry safe
- `project_state` has `UNIQUE(project_id, client_id, bot_id)` — only one state row per project
- All JSON blobs stored as TEXT; deserialized on read by the model layer
