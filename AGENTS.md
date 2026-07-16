# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Build / Run / Test

```bash
# Install dependencies (uv is the package manager)
uv venv --python 3.12
source .venv/bin/activate
uv pip install -r requirements.txt

# Copy and edit config
cp .env.example .env

# Run the server
uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-level info

# Docker
docker-compose up -d
```

There are no test files or linting configuration in this project.

## Architecture

This is a **Feishu (Lark) webhook-driven AI scoring service**. It listens for Bitable record change events, collects content from multiple sources (text fields, Feishu docs, attachments), sends everything to an AI model for scoring, writes scores back to the Bitable, and notifies the submitter via Feishu bot messages.

**Request flow:**

```
Feishu Bitable record changed event
  → POST /webhook/event (main.py)
    → lark-oapi SDK decrypts & dispatches by event type
      → Orchestrator.process_record() (orchestrator.py)
        → FeishuClient: read record fields, fetch doc content, download attachments
        → document_parser: extract text from PDF/DOCX/MD attachments
        → AIClient: send system+user prompt to AI, parse JSON response
        → FeishuClient: write score/detail/status back to record
        → NotificationManager: send pass/fail/rejected card message (if rate limits allow)
```

**Module responsibilities:**

| Module | Role |
|---|---|
| `app/main.py` | FastAPI app, lifespan, `/webhook/event` endpoint, event deduplication |
| `app/orchestrator.py` | State machine (`待评分 → 评分中 → 已通过/未通过 → loop/reject`), coordinates all steps for a single record |
| `app/feishu.py` | All Feishu Open API calls (Bitable record CRUD, Doc raw content, IM messages, attachment download) via `lark-oapi` SDK |
| `app/ai.py` | Multi-provider AI scoring (OpenAI, Codex, DeepSeek, Doubao), JSON parsing with fallback strategies |
| `app/notification.py` | Feishu card message templates, per-record cooldown (default 60 min), per-user daily cap (default 3) |
| `app/parser.py` | Extract text from `pdfplumber`, `python-docx`, text/MD files; parse Feishu document IDs from URLs; smart truncation (head 60% + tail 20%) |
| `app/config.py` | All env vars as a `@dataclass`, `get_config()` is a cached singleton that validates required fields |
| `app/field_mapping.py` | Constants mapping Feishu Bitable field names to Python identifiers — edit this to match your table schema |

**Key design decisions:**

- **Event deduplication**: `EventDeduplicator` in `main.py` caches event IDs for 5 minutes to handle Feishu's at-least-once delivery.
- **Concurrency guard**: `Orchestrator._processing` set prevents the same record from being processed concurrently (in-memory only, lost on restart).
- **State machine gating**: Only records in `待评分` or `未通过` state trigger scoring; other states (`评分中`, `已通过`, `已驳回`) are silently skipped. On AI failure, status is restored to the previous state.
- **AI JSON parsing**: Three-tier fallback: direct `json.loads` → regex extract JSON block → regex extract `"score"` field. Sets `_parse_fallback: True` in the result dict when degraded.
- **AI provider abstraction**: All four providers use a `score()` method returning `{score, detail, dimensions}`; the provider is selected by `AI_PROVIDER` env var. OpenAI-compatible providers (OpenAI, DeepSeek, Doubao) use the `openai` SDK; Codex uses the `anthropic` SDK.
- **Attachment MIME type guessing**: Falls back to filename extension when MIME type is missing from Feishu's metadata.
- **Content truncation**: `truncate_content()` in `document_parser.py` caps at 8000 chars by default, keeping the head 60% and tail 20% with an omission marker.

**To add a new AI provider:**
1. Add a new `_call_<provider>` method in `AIClient` returning `str | None`
2. Add an `elif self._provider == "<name>":` branch in `score()`
3. Add the provider name to the env var comment in `.env.example`

**To change field mappings:** Edit `field_mapping.py` — the field name constants are used throughout `orchestrator.py`.

**To modify scoring criteria:** Edit `SCORING_SYSTEM_PROMPT` in `ai_client.py`. The expected JSON response schema should stay consistent: `{score, detail, dimensions: {completeness, logic, format, quality}}`.

## Feishu CLI (lark-cli)

```bash
# One-time setup (requires Node.js >= 20.12 via nvm)
nvm install --lts
npm install -g @larksuite/cli
lark-cli config init --app-id <FEISHU_APP_ID> --app-secret-stdin --brand feishu
```

The CLI is useful for creating/managing Bitables, testing API calls, and debugging permissions.

## Local Development with Webhooks

Feishu webhooks require a public HTTPS URL. For local dev, use ngrok:

```bash
ngrok http 8000
# → https://xxxx.ngrok-free.dev → configure as webhook URL in Feishu console
```

## Required Feishu App Scopes

| Scope | Purpose |
|---|---|
| `base:app:create` | Create Bitables via API |
| `docs:event:subscribe` | Subscribe to Bitable record change events |
| `docx:document` / `docx:document:readonly` | Read Feishu document raw content |
| `bitable:app` | Read/write Bitable records |
| `drive:drive` | File/media download (attachments) |
| `im:message:send_as_bot` | Send bot notifications |

## Critical: Document Event Subscription

**Feishu cloud document events require an explicit API call BEFORE events will be delivered.** Just adding the event type in the Feishu console is NOT enough:

```python
from lark_oapi.api.drive.v1 import SubscribeFileRequest
req = SubscribeFileRequest.builder() \
    .file_token("<BITABLE_APP_TOKEN>") \
    .file_type("bitable") \
    .build()
client.drive.v1.file.subscribe(req)
```

Without this call, record changes will never be pushed to the webhook.

## Common Pitfalls Fixed

1. **Event model mismatch**: The `P2DriveFileBitableRecordChangedV1` event has NO top-level `record_id`. Record IDs are in `event.action_list[i].record_id`. Each event can contain multiple record actions (`record_added`, `record_edited`, `record_deleted`).

2. **FastAPI header case**: `dict(request.headers)` lowercases all header names, but the lark-oapi SDK expects mixed-case (`X-Lark-Request-Timestamp`, `X-Lark-Request-Nonce`, `X-Lark-Signature`). These must be remapped before passing to the SDK.

3. **Bitable datetime format**: Feishu Bitable datetime fields expect **Unix timestamps in milliseconds** (int), NOT ISO 8601 strings.

4. **Attachment download auth**: The `download_attachment` method must include the tenant access token in the `Authorization` header. Raw `httpx.get()` without auth returns 400.

5. **`registration_p2_drive_file_bitable_record_changed_v1`**: The handler is for retroactive (补发) events. For normal events, the `register_p2_drive_file_bitable_record_changed_v1` handler is used. Both should be registered for full coverage.
