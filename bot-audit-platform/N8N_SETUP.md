# n8n Setup Guide

n8n is the orchestration layer. It receives handoff payloads from your bot, writes to the audit store, and routes code changes to workers (bash, Claude Code, Codex).

---

## Install n8n

**Requires Node 20 — does not work on Node 25.**

```bash
# If you have nvm:
nvm install 20
nvm use 20
npm install -g n8n

# Verify
n8n --version  # should print 2.x.x
```

If you don't have nvm:
```bash
# macOS
brew install nvm
mkdir -p ~/.nvm
export NVM_DIR="$HOME/.nvm"
source $(brew --prefix nvm)/nvm.sh
nvm install 20
nvm use 20
npm install -g n8n
```

**Add to `~/.zshrc` or `~/.bashrc` to make it permanent:**
```bash
export NVM_DIR="$HOME/.nvm"
[ -s "$(brew --prefix nvm)/nvm.sh" ] && source "$(brew --prefix nvm)/nvm.sh"
nvm use 20 --silent
```

---

## Start n8n

```bash
# Default: runs on http://localhost:5678
n8n start

# With a custom data directory (recommended):
N8N_USER_FOLDER=/path/to/n8n-data n8n start
```

Open `http://localhost:5678` and complete the initial setup.

---

## Configure Webhook URLs

Set these environment variables in your bot's environment:

```bash
export N8N_AUDIT_WEBHOOK_URL=http://localhost:5678/webhook/audit
export N8N_EXECUTION_WEBHOOK_URL=http://localhost:5678/webhook/execute
```

---

## Workflow 1: Audit Webhook (session close)

This workflow receives the `audit_record` handoff from your bot after each session.

### Nodes:
```
[Webhook: POST /webhook/audit]
    ↓
[Function: Validate payload]
    ↓
[Execute Command: write audit event to SQLite]
    ↓
[Execute Command: update project state]
    ↓
[IF: errors?]
    ↓ yes         ↓ no
[DLQ enqueue]  [Respond: 200 OK]
```

### Webhook node config:
- **HTTP Method:** POST
- **Path:** `audit`
- **Authentication:** None (or add header auth)
- **Respond with:** `Last Node`

### Function node (validate payload):
```javascript
const payload = $json.body;
if (!payload.handoff_type || payload.handoff_type !== 'audit_record') {
  throw new Error('Invalid handoff_type');
}
if (!payload.bot_id || !payload.session_id) {
  throw new Error('Missing required fields');
}
return [{ json: payload }];
```

### Execute Command node (write audit event):
```bash
python3 /path/to/bot-audit-platform/scripts/write_audit_event.py \
  --payload '{{ $json.body | json }}'
```

> **Phase 1 shortcut:** Skip n8n entirely by not setting `N8N_AUDIT_WEBHOOK_URL`. The bot will write directly to SQLite. Use n8n when you need the full automation pipeline.

---

## Workflow 2: Execution Webhook (code change request)

This workflow receives `execution` handoffs and routes to the appropriate worker.

### Nodes:
```
[Webhook: POST /webhook/execute]
    ↓
[Function: Check approval_required]
    ↓ approved (or not required)
[Switch: route by worker]
    ├─ bash → [Execute Command: run bash script]
    ├─ claude_code → [Execute Command: claude-worker.sh]
    │                     ↓
    │                [IF: review required?]
    │                     ↓ yes
    │                [HTTP Request: Codex review]
    │                     ↓
    │                [IF: approved?]
    │                     ↓ yes      ↓ no
    │                [Deploy]    [Retry once or DLQ]
    └─ n8n_direct → [Execute deploy script]
```

### Switch node routing:
```javascript
// Output 0: bash
const worker = $json._routing.worker;
if (worker === 'bash') return 0;
// Output 1: claude_code
if (worker === 'claude_code') return 1;
// Output 2: n8n_direct
return 2;
```

---

## Workflow 3: Claude Code Worker

Configure an **Execute Command** node to run `claude-worker.sh`:

```bash
bash /path/to/bot-audit-platform/scripts/claude-worker.sh \
  "{{ $json.instructions.repo_ref }}" \
  "/tmp/prompt_{{ $json.correlation_id }}.txt" \
  "/tmp/result_{{ $json.correlation_id }}.json"
```

**Before this node**, add a **Write Binary File** node to write the prompt:
```javascript
// Build prompt from instructions
const instr = $json.instructions;
const prompt = `${instr.change_specification}

Target files: ${instr.target_files.join(', ')}

Acceptance criteria:
${instr.acceptance_criteria}

Commit message template: ${instr.commit_message_template}`;

return [{ json: { ...$json, prompt_text: prompt } }];
```

**After this node**, read the JSON result file:
```javascript
const resultPath = `/tmp/result_${$json.correlation_id}.json`;
// Read and parse the output file
```

---

## Workflow 4: Codex Review

After Claude Code commits, call Codex via HTTP Request node:

**HTTP Request node config:**
- **Method:** POST
- **URL:** `https://api.openai.com/v1/chat/completions`
- **Headers:**
  - `Authorization: Bearer {{ $env.OPENAI_API_KEY }}`
  - `Content-Type: application/json`
- **Body:**
```json
{
  "model": "codex-mini-latest",
  "messages": [
    {
      "role": "system",
      "content": "You are a code reviewer. Return ONLY valid JSON matching the verdict schema."
    },
    {
      "role": "user",
      "content": "INSTRUCTIONS: {{ $json.instructions.change_specification }}\n\nTARGET FILES: {{ $json.instructions.target_files.join(', ') }}\n\nGIT DIFF:\n{{ $json.git_diff }}\n\nReturn verdict JSON: {approved, confidence, scope_match, spec_match, criteria_met, issues, regression_risk, summary}"
    }
  ],
  "max_tokens": 1024,
  "temperature": 0.1
}
```

---

## Workflow 5: Idle Timeout Cron

Add a **Schedule** node to check for sessions that have exceeded the idle timeout:

```
[Schedule: every 5 minutes]
    ↓
[Execute Command: python3 check_idle.py]
    ↓
[IF: sessions timed out?]
    ↓ yes
[HTTP Request: POST /webhook/audit with closed session]
```

`check_idle.py`:
```python
from bot_audit.audit import SessionManager
# Check idle timeouts for all active sessions
# Each timed-out session triggers run_post_session()
```

---

## DLQ in n8n

Add a **Schedule** node to retry DLQ items:

```
[Schedule: every 15 minutes]
    ↓
[Execute Command: python3 /path/to/mybot/run_post_session.py --retry-dlq]
```

---

## Environment Variables for n8n

Set these in n8n's environment or in your `.env` file:

```bash
OPENROUTER_API_KEY=sk-or-...   # For LLM calls routed through the platform
OPENAI_API_KEY=sk-...           # For direct Codex calls from n8n HTTP Request node
N8N_AUDIT_WEBHOOK_URL=http://localhost:5678/webhook/audit
N8N_EXECUTION_WEBHOOK_URL=http://localhost:5678/webhook/execute
```

---

## Production Considerations

1. **Authentication:** Add header-based auth to all webhooks (n8n supports `Header Auth` credential)
2. **SSL:** Put n8n behind nginx with TLS if exposed externally
3. **Persistence:** Use `N8N_USER_FOLDER` to store n8n data outside the install directory
4. **Process manager:** Run n8n under PM2 or systemd for auto-restart
5. **Timeout:** The `claude-worker.sh` timeout is 180 seconds — set the Execute Command node timeout to 200s
