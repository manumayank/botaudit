#!/bin/bash
# claude-worker.sh — Claude Code execution wrapper for n8n
#
# v4 Architecture §3.3 — Tier 2: Claude Code CLI (Intelligent Changes)
#
# CRITICAL PRINCIPLE: TRUST GIT, NOT CLAUDE
#   This wrapper derives ALL output fields from git state, not from Claude Code's
#   self-reported output. Claude may claim it changed 2 files when it changed 3.
#   The wrapper checks: (1) clean working tree, (2) exit code, (3) actual new commit,
#   (4) files changed via git diff. n8n parses the wrapper's JSON output file.
#
# Usage:
#   bash claude-worker.sh <project_dir> <prompt_file> <output_file>
#
# Args:
#   project_dir   Absolute path to git repo (e.g. /repos/bookswap)
#   prompt_file   Path to file containing the change prompt (avoid shell escaping issues)
#   output_file   Path where structured JSON result will be written
#
# Output JSON (written to output_file):
#   { "success": true,  "pre_hash": "...", "commit_hash": "...",
#     "files_changed": [...], "diff_stat": "..." }
#   { "success": false, "error": "<reason>" }
#
# n8n invokes this via Execute Command node with timeout: 180s
# If this script exits non-zero, n8n routes to dead letter queue.

set -euo pipefail

PROJECT_DIR="${1:?Usage: claude-worker.sh <project_dir> <prompt_file> <output_file>}"
PROMPT_FILE="${2:?}"
OUTPUT_FILE="${3:?}"

# Validate inputs
if [ ! -d "$PROJECT_DIR/.git" ]; then
    echo '{"success":false,"error":"not a git repository"}' > "$OUTPUT_FILE"
    exit 1
fi

if [ ! -f "$PROMPT_FILE" ]; then
    echo '{"success":false,"error":"prompt file not found"}' > "$OUTPUT_FILE"
    exit 1
fi

cd "$PROJECT_DIR"

# Capture pre-change state
PRE_HASH=$(git rev-parse HEAD)
PRE_STATUS=$(git status --porcelain)

# Require clean working tree before starting
if [ -n "$PRE_STATUS" ]; then
    echo '{"success":false,"error":"dirty working tree — uncommitted changes present"}' > "$OUTPUT_FILE"
    exit 1
fi

# Run Claude Code in non-interactive print mode
# Prompt is read from file to avoid shell escaping issues with special characters
claude --print < "$PROMPT_FILE" > /tmp/claude_raw_output.txt 2>&1
CLAUDE_EXIT=$?

# Capture post-change state
POST_HASH=$(git rev-parse HEAD 2>/dev/null || echo "$PRE_HASH")
POST_STATUS=$(git status --porcelain)

# Handle Claude Code failure
if [ $CLAUDE_EXIT -ne 0 ]; then
    # Restore repo to clean state
    git reset --hard "$PRE_HASH" 2>/dev/null || true
    STDERR_SNIPPET=$(head -c 200 /tmp/claude_raw_output.txt | tr '\n' ' ' | tr '"' "'")
    echo "{\"success\":false,\"error\":\"claude exit $CLAUDE_EXIT: $STDERR_SNIPPET\"}" > "$OUTPUT_FILE"
    exit 1
fi

# Check if any changes were actually made
if [ "$PRE_HASH" = "$POST_HASH" ] && [ -z "$POST_STATUS" ]; then
    echo '{"success":false,"error":"no changes made — claude may have misunderstood the prompt"}' > "$OUTPUT_FILE"
    exit 1
fi

# If there are uncommitted changes (Claude didn't commit), commit them now
if [ -n "$POST_STATUS" ] && [ "$PRE_HASH" = "$POST_HASH" ]; then
    # Try to extract commit message hint from the prompt file
    COMMIT_MSG=$(grep -i "commit" "$PROMPT_FILE" | head -1 | sed 's/.*commit.*://i' | tr -d '"' | cut -c1-100 || echo "chore: automated change")
    git add -A
    git commit -m "${COMMIT_MSG:-chore: claude code change}" || {
        git reset --hard "$PRE_HASH"
        echo '{"success":false,"error":"git commit failed after claude changes"}' > "$OUTPUT_FILE"
        exit 1
    }
    POST_HASH=$(git rev-parse HEAD)
fi

# Derive output from git truth (NOT from Claude's claims)
COMMIT_HASH=$(git rev-parse HEAD)
DIFF_STAT=$(git diff --stat "$PRE_HASH" HEAD 2>/dev/null | tail -1 | tr '"' "'")

# Build files_changed as JSON array from git diff
FILES_JSON=$(git diff --name-only "$PRE_HASH" HEAD | python3 -c "
import sys, json
files = [l.strip() for l in sys.stdin if l.strip()]
print(json.dumps(files))
" 2>/dev/null || echo '[]')

# Write structured output
python3 - << PYEOF
import json
result = {
    "success": True,
    "pre_hash": "$PRE_HASH",
    "commit_hash": "$COMMIT_HASH",
    "files_changed": $FILES_JSON,
    "diff_stat": "$DIFF_STAT"
}
with open("$OUTPUT_FILE", "w") as f:
    json.dump(result, f, indent=2)
PYEOF

echo "claude-worker: success. commit=$COMMIT_HASH files=$(echo $FILES_JSON | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))')"
