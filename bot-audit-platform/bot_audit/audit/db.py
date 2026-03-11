"""
SQLite database setup and schema for the audit/state platform.

All tables use append-only semantics except project_state
(which is a materialized view, updated with optimistic locking).
"""

import sqlite3
from pathlib import Path


def get_db(db_path: str) -> sqlite3.Connection:
    """Open (and initialize) the SQLite database."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _initialize_schema(conn)
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    """Create all tables. Safe to call on an already-initialized DB."""
    conn.executescript("""
        -- Append-only event log: the foundational source of truth.
        -- Never UPDATE or DELETE rows here.
        CREATE TABLE IF NOT EXISTS events (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id          TEXT UNIQUE NOT NULL,
            event_type        TEXT NOT NULL,
            timestamp         TEXT NOT NULL,
            bot_id            TEXT NOT NULL,
            client_id         TEXT NOT NULL,
            project_id        TEXT NOT NULL,
            session_id        TEXT,
            actor             TEXT NOT NULL,
            schema_version    INTEGER NOT NULL DEFAULT 1,
            correlation_id    TEXT,
            causation_id      TEXT,
            idempotency_key   TEXT UNIQUE NOT NULL,
            created_by_type   TEXT NOT NULL,
            payload           TEXT NOT NULL,   -- JSON blob
            confidence        REAL,
            parent_event_id   TEXT,
            created_at        TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_events_bcp
            ON events(bot_id, client_id, project_id);
        CREATE INDEX IF NOT EXISTS idx_events_session
            ON events(session_id);
        CREATE INDEX IF NOT EXISTS idx_events_type
            ON events(event_type);
        CREATE INDEX IF NOT EXISTS idx_events_correlation
            ON events(correlation_id);
        CREATE INDEX IF NOT EXISTS idx_events_timestamp
            ON events(timestamp);

        -- Tracks the state of each session (one per bot+client pair).
        -- Only one row per (bot_id, client_id) can have status='open'.
        CREATE TABLE IF NOT EXISTS session_state (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id              TEXT UNIQUE NOT NULL,
            bot_id                  TEXT NOT NULL,
            client_id               TEXT NOT NULL,
            project_id              TEXT NOT NULL,
            status                  TEXT NOT NULL DEFAULT 'open',
            correlation_id          TEXT NOT NULL,
            opened_at               TEXT NOT NULL,
            last_message_at         TEXT NOT NULL,
            closed_at               TEXT,
            close_reason            TEXT,
            transcript_path         TEXT,
            intent_classifications  TEXT NOT NULL DEFAULT '[]',  -- JSON array
            message_count           INTEGER NOT NULL DEFAULT 0,
            created_at              TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_session_active
            ON session_state(bot_id, client_id)
            WHERE status = 'open';

        CREATE INDEX IF NOT EXISTS idx_session_bcp
            ON session_state(bot_id, client_id, project_id);

        -- Materialized view of project state.
        -- Updated after each session audit via optimistic locking.
        -- NEVER write to this table without reading + incrementing status_version.
        CREATE TABLE IF NOT EXISTS project_state (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id       TEXT NOT NULL,
            client_id        TEXT NOT NULL,
            bot_id           TEXT NOT NULL,
            current_status   TEXT NOT NULL DEFAULT 'active',
            status_version   INTEGER NOT NULL DEFAULT 0,
            last_updated     TEXT NOT NULL,
            last_session_ref TEXT,
            state_data       TEXT NOT NULL DEFAULT '{}',   -- JSON blob
            created_at       TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(project_id, client_id, bot_id)
        );

        -- Dead letter queue: failed post-session processing.
        -- Every raw transcript is preserved here regardless of downstream failures.
        CREATE TABLE IF NOT EXISTS dead_letter (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            dead_letter_id   TEXT UNIQUE NOT NULL,
            original_event   TEXT NOT NULL,     -- JSON blob
            failure_reason   TEXT NOT NULL,
            failure_stage    TEXT NOT NULL,     -- audit_write|state_update|n8n_trigger|...
            retry_count      INTEGER NOT NULL DEFAULT 0,
            max_retries      INTEGER NOT NULL DEFAULT 3,
            next_retry_at    TEXT,
            status           TEXT NOT NULL DEFAULT 'pending',  -- pending|retrying|resolved|abandoned
            transcript_ref   TEXT NOT NULL,
            created_at       TEXT NOT NULL DEFAULT (datetime('now')),
            owner            TEXT,
            alert_severity   TEXT NOT NULL DEFAULT 'warning',
            last_error_class TEXT,
            escalated        INTEGER NOT NULL DEFAULT 0
        );

        -- Denormalized review_events for fast query of Codex verdicts.
        -- Written by n8n after each Codex review. Read by OpenClaw retrieval.
        -- Every row is also in the events table as event_type='review_event'.
        CREATE TABLE IF NOT EXISTS review_events (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            review_event_id         TEXT UNIQUE NOT NULL,   -- event_id from events table
            correlation_id          TEXT NOT NULL,
            bot_id                  TEXT NOT NULL,
            client_id               TEXT NOT NULL,
            project_id              TEXT NOT NULL,
            reviewer                TEXT NOT NULL,          -- codex | claude | human
            reviewer_model          TEXT,
            originating_action_id   TEXT,                   -- event_id of action_event reviewed
            approved                INTEGER NOT NULL,        -- 0/1 boolean
            confidence              REAL,
            scope_match             INTEGER,
            spec_match              INTEGER,
            criteria_met            INTEGER,
            risk_assessment_match   INTEGER,
            risk_escalation         TEXT,
            regression_risk         TEXT,
            verdict_data            TEXT NOT NULL,          -- full JSON verdict
            retry_number            INTEGER NOT NULL DEFAULT 0,
            timestamp               TEXT NOT NULL,
            created_at              TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_review_events_bcp
            ON review_events(bot_id, client_id, project_id);
        CREATE INDEX IF NOT EXISTS idx_review_events_correlation
            ON review_events(correlation_id);

        -- Pipeline pause state. One row per scope.
        -- Operators set paused=1 to halt execution handoffs.
        CREATE TABLE IF NOT EXISTS pipeline_pause (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scope       TEXT NOT NULL,          -- global | project | bot
            scope_id    TEXT NOT NULL,          -- '*' for global, project_id, or bot_id
            paused      INTEGER NOT NULL DEFAULT 0,
            reason      TEXT,
            paused_by   TEXT,
            paused_at   TEXT,
            resumed_at  TEXT,
            UNIQUE(scope, scope_id)
        );

        -- Handoff hold queue: payloads queued while pipeline is paused.
        -- NOT the dead letter queue — these are valid payloads held intentionally.
        CREATE TABLE IF NOT EXISTS hold_queue (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            hold_id         TEXT UNIQUE NOT NULL,
            correlation_id  TEXT NOT NULL,
            bot_id          TEXT NOT NULL,
            project_id      TEXT NOT NULL,
            payload         TEXT NOT NULL,      -- JSON handoff payload
            queued_at       TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'held',   -- held | released | discarded
            released_at     TEXT
        );
    """)
    conn.commit()
