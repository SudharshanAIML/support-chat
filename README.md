# Support Chat API

A conversational AI service embedded in the CRM that gives employees three distinct
interaction modes over their data: help & navigation (ASK), data insights (VISUALIZE),
and autonomous CRM actions (AGENT).

Built with **FastAPI**, **LangGraph**, **Groq (llama-3.3-70b)**, **ChromaDB** (RAG),
and **SQLAlchemy** against a MySQL backend.

---

## Table of Contents

1. [System Architecture](#1-system-architecture)
2. [How the Three Modes Work](#2-how-the-three-modes-work)
3. [Setup & Running](#3-setup--running)
4. [Knowledge Base (RAG) Ingestion](#4-knowledge-base-rag-ingestion)
5. [API Endpoints — Full Reference](#5-api-endpoints--full-reference)
   - [System](#51-system)
   - [Sessions](#52-sessions)
   - [Chat](#53-chat)
   - [MCP Server](#54-mcp-server)
6. [Request & Response Schemas](#6-request--response-schemas)
7. [Authentication](#7-authentication)
8. [Rate Limiting](#8-rate-limiting)
9. [Background Services](#9-background-services)
10. [Docker](#10-docker)
11. [Tests](#11-tests)
12. [Environment Variables](#12-environment-variables)

---

## 1. System Architecture

```
CRM Frontend
     │  JWT forwarded in Authorization header
     ▼
Support Chat API  (FastAPI, port 8000)
     │
     ├── /sessions/*      ── Session management (create, read, history, delete)
     │
     └── /sessions/{id}/chat  ── Single chat endpoint, mode-dispatched
              │
              ├── ASK mode  ── ChromaDB RAG → Groq LLM
              │               (CRM_DOCUMENTATION.md chunks, no DB queries)
              │
              ├── VISUALIZE  ── Groq LLM → SQL generation → tenant-scoped execution
              │               → chart spec for frontend rendering
              │
              └── AGENT mode ── LangGraph ReAct → CRM REST API tools
                              (contacts, tasks, email, automations)
```

**Persistent storage:**
- `DATABASE_URL` (MySQL via SQLAlchemy) — session records, message history, tool audit log
- `.chroma/` — ChromaDB vector store for ASK mode RAG

---

## 2. How the Three Modes Work

### 2.1 ASK Mode — RAG-grounded Help

**Purpose:** Answer questions about how the CRM works, where features live, and what
pipeline rules apply.  No database queries, no side effects.

**Workflow:**

```
User message
     │
     ▼
Guardrail check (out-of-scope detection)
     │ (in scope)
     ▼
ChromaDB.retrieve(query, top_k=5)
     │ returns ranked document chunks from CRM_DOCUMENTATION.md
     ▼
Groq LLM (llama-3.3-70b, temp=0.2)
     │ system prompt: answer only from the retrieved snippets
     ▼
Response with source citations
```

**What it knows:** Everything in `CRM_DOCUMENTATION.md` — pipeline stages, transition
rules, API endpoints, UI navigation, database schema, business thresholds.

**When ASK falls back:** If no chunks are retrieved, the assistant tells the user
no documentation is indexed yet and suggests rephrasing or asking an admin to add docs.

**Auth requirement:** None — ASK works without a JWT.

---

### 2.2 VISUALIZE Mode — Read-only Data Insights

**Purpose:** Answer business questions by generating and running a SQL SELECT query
against the CRM database, returning rows plus a chart specification the frontend renders.

**Workflow:**

```
User message + company_id (from JWT)
     │
     ▼
Groq LLM — generates:
  · ONE read-only SQL SELECT (never INSERT/UPDATE/DELETE)
  · chart spec { type, x, y, aggregate, title }
     │
     ▼
Tenant guard — enforce company_id = <id> in every table with company_id
     │ (rejects if missing; protects cross-tenant data leakage)
     ▼
SQLAdapter.execute(sql)  ── capped at 500 rows
     │
     ▼
Response: { content, executed_query, query_result, visualization }
```

**Chart types supported:** `bar`, `line`, `pie`, `area`, `scatter`, `table`, `number`

**Auth requirement:** Valid employee JWT with `companyId` claim.

---

### 2.3 AGENT Mode — Autonomous CRM Actions

**Purpose:** Execute multi-step CRM operations on behalf of the signed-in employee
using a LangGraph ReAct loop with tool-calling.

**Workflow:**

```
User message + employee JWT
     │
     ▼
Build CRM client (JWT forwarded to every CRM API call)
Build tool set (contacts, tasks, email, automations)
     │
     ▼
LangGraph ReAct loop (max AGENT_MAX_STEPS=10):
  ┌─ LLM decides which tool(s) to call
  │
  ├─ ToolNode executes the tool
  │     · Read tools: execute immediately
  │     · Destructive tools (email send, deal close, create automation):
  │         return CONFIRM_MARKER sentinel — loop halts
  │
  └─ Loop until: no more tool calls  OR  confirmation needed  OR  step cap
     │
     ▼
If confirmation needed:
  → Response: { requires_confirmation: true, pending_action: { tool, tool_input, prompt } }
  → Client resends same message with { confirmed: true }
  → Tool executes for real

If done:
  → Response: { content, agent_reasoning, tool_results }
  → Tool calls written to tool_audit table (immutable audit log)
```

**Available tools:**

| Tool | CRM Endpoints Used | Confirmation Required |
|------|--------------------|-----------------------|
| `search_contacts` | `GET /api/contacts/search` | No |
| `get_contact` | `GET /api/contacts/:id` | No |
| `update_contact` | `PATCH /api/contacts/:id` | No |
| `create_task` | `POST /api/tasks` | No |
| `list_tasks` | `GET /api/tasks/today` / `GET /api/tasks/week` | No |
| `send_email` | `POST /api/emails` | **Yes** |
| `automation_metadata` | `GET /api/automations/metadata` | No |
| `create_automation` | `POST /api/automations` | **Yes** |

**Auth requirement:** Valid employee JWT (forwarded from the CRM).

---

## 3. Setup & Running

### Prerequisites

- Python 3.11+
- MySQL 8.x (or Aiven Cloud MySQL)
- A [Groq](https://console.groq.com) API key

### Install

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
# Edit .env — minimum required:
#   GROQ_API_KEY=gsk_...
#   DATABASE_URL=mysql+pymysql://user:pass@host:port/dbname
#   API_KEYS=your-secret-key
```

### Run database migrations

```bash
alembic upgrade head
```

### Ingest the CRM knowledge base (ASK mode)

```bash
python -m app.rag.ingest
# or to wipe and re-ingest:
python -m app.rag.ingest --reset
```

`CRM_DOCUMENTATION.md` in the project root is automatically included.  Additional
`.md` / `.txt` files can be placed in the `./knowledge/` folder.

### Start the server

```bash
uvicorn app.main:app --reload
```

Interactive API docs: **http://127.0.0.1:8000/docs**

---

## 4. Knowledge Base (RAG) Ingestion

The ASK mode retrieves grounding chunks from a local ChromaDB store.  `ingest.py`
handles the document pipeline:

```
Document (CRM_DOCUMENTATION.md or ./knowledge/*.md)
     │
     ▼
_split_text()  — overlapping 1 200-char windows (150-char overlap),
                 preferring paragraph / sentence breaks
     │
     ▼
SentenceTransformer("all-MiniLM-L6-v2")  — local, no API key
     │
     ▼
ChromaDB.upsert(ids, documents, metadatas)
     │  stored in ./.chroma/
     ▼
ASK mode calls ChromaDB.query(question, n_results=5)
```

**Ingest commands:**

```bash
# Ingest default sources (CRM_DOCUMENTATION.md + ./knowledge/)
python -m app.rag.ingest

# Ingest a custom folder
python -m app.rag.ingest --path ./my-docs

# Wipe the collection and re-ingest from scratch
python -m app.rag.ingest --reset
```

**Re-ingest whenever `CRM_DOCUMENTATION.md` changes** so ASK mode has up-to-date
information.

---

## 5. API Endpoints — Full Reference

All endpoints except `GET /` and `GET /health` require the `X-API-Key` header.

### 5.1 System

#### `GET /`

**Access:** Public

Returns service name and version.

```json
{ "Artifact": "Support Chat", "version": "0.1.0" }
```

---

#### `GET /health`

**Access:** Public

Lightweight liveness probe — returns 200 if the process is running.

```json
{ "status": "healthy", "version": "0.1.0" }
```

---

### 5.2 Sessions

Sessions maintain conversation history and schema context across turns.  The CRM
creates one session per user chat window and forwards the `session_id` on every message.

#### `POST /sessions`

**Access:** API key required

Create a new chat session.  The schema context is used by VISUALIZE mode when generating
SQL.  Optionally supply a `db_url` to enable live query execution and auto-discover the
schema automatically.

**Request body:**

```json
{
  "query_type": "mysql",          // mysql | postgresql | sqlite | sql
  "schema_context": [...],        // optional if db_url is provided
  "db_url": "mysql+pymysql://...", // optional — enables live query execution
  "system_instructions": "..."    // optional custom instructions for the LLM
}
```

**Workflow:**

1. If `db_url` is provided and `query_type` is a SQL dialect, the server calls
   `introspect_schema()` to discover tables and columns from the live database.
2. Discovered schema replaces any manual `schema_context`.
3. If auto-discovery fails and no manual `schema_context` was provided, returns `400`.
4. Session is written to the `chat_sessions` table with a UUID `session_id`.

**Response `201`:**

```json
{
  "session_id": "uuid",
  "created_at": "2025-01-01T00:00:00Z",
  "query_type": "mysql",
  "has_db_connection": true
}
```

---

#### `GET /sessions/{session_id}`

**Access:** API key required

Return session metadata (does not include messages).

**Response `200`:**

```json
{
  "session_id": "uuid",
  "created_at": "...",
  "query_type": "mysql",
  "message_count": 12,
  "has_db_connection": true
}
```

---

#### `GET /sessions/{session_id}/history`

**Access:** API key required

Return the full ordered conversation history for the session.

**Response `200`:**

```json
{
  "session_id": "uuid",
  "messages": [
    {
      "role": "user",
      "content": "How many leads do I have?",
      "query": null,
      "query_result": null,
      "insight": null,
      "timestamp": "..."
    },
    {
      "role": "assistant",
      "content": "You have 42 leads.",
      "query": "SELECT COUNT(*) AS total FROM contacts WHERE ...",
      "query_result": [{ "total": 42 }],
      "insight": null,
      "timestamp": "..."
    }
  ]
}
```

---

#### `DELETE /sessions/{session_id}`

**Access:** API key required

Terminate a session and remove all conversation history from the database.

**Response:** `204 No Content`

---

### 5.3 Chat

The single chat endpoint handles all three modes.  The CRM frontend always sends the
employee's JWT in the `Authorization: Bearer <token>` header; the support-chat service
forwards it to CRM API calls made by AGENT mode tools.

#### `POST /sessions/{session_id}/chat`

**Access:** API key + (for VISUALIZE / AGENT) valid employee JWT

Send one conversation turn and receive the assistant response.

**Request body:**

```json
{
  "message": "Show me the top 5 contacts by deal value",
  "mode": "visualize",         // ask | visualize | agent
  "confirmed": false           // set true to confirm a pending AGENT action
}
```

**Headers:**

| Header | Required | Purpose |
|--------|----------|---------|
| `X-API-Key` | Always | Service authentication |
| `Authorization` | VISUALIZE & AGENT | Employee JWT forwarded from the CRM |

**Workflow by mode:**

| Mode | Steps | Side Effects |
|------|-------|-------------|
| `ask` | Guardrail → ChromaDB retrieve → LLM answer | None |
| `visualize` | LLM generates SQL + chart spec → tenant guard → execute → return rows | None (read-only) |
| `agent` | ReAct loop (LLM + tools) → confirmation gate → tool execution | CRM mutations, tool audit log written |

**Response `200`:**

```json
{
  "role": "assistant",
  "mode": "visualize",
  "content": "Here are the top 5 contacts by deal value.",
  "query": "SELECT c.name, d.deal_value FROM contacts c ...",
  "query_result": [
    { "name": "Acme Corp", "deal_value": 50000 },
    ...
  ],
  "visualization": {
    "chart_type": "bar",
    "x": "name",
    "y": "deal_value",
    "aggregate": "none",
    "title": "Top 5 Contacts by Deal Value",
    "row_count": 5
  },
  "agent_reasoning": null,
  "tool_results": null,
  "requires_confirmation": false,
  "pending_action": null,
  "sources": null,
  "error": null
}
```

**ASK mode response includes `sources`:**

```json
{
  "mode": "ask",
  "content": "To promote a LEAD to MQL, open the contact and click ...",
  "sources": [
    { "source": "CRM_DOCUMENTATION.md", "distance": 0.12 }
  ]
}
```

**AGENT mode confirmation flow:**

First call (destructive tool discovered):
```json
{
  "mode": "agent",
  "content": "This will send an email to john@example.com. Confirm?",
  "requires_confirmation": true,
  "pending_action": {
    "tool": "send_email",
    "tool_input": { "to": "john@example.com", "subject": "...", "body": "..." },
    "prompt": "This will send an email to john@example.com. Confirm?"
  },
  "agent_reasoning": [
    { "step": 1, "thought": "", "tool_name": "search_contacts", "tool_input": {...} }
  ]
}
```

Second call with `confirmed: true`:
```json
{
  "mode": "agent",
  "content": "Email sent to john@example.com.",
  "requires_confirmation": false,
  "pending_action": null,
  "tool_results": [
    { "tool_call_id": "...", "result": { "success": true } }
  ]
}
```

**Error responses:**

| Status | Condition |
|--------|-----------|
| `404` | Session not found or expired |
| `401` | VISUALIZE/AGENT called without a valid JWT |
| `422` | Missing required fields |

---

### 5.4 MCP Server

When the application starts, a Model Context Protocol (MCP) server is mounted at
`/mcp` (SSE transport).  This exposes the query translation and session management
capabilities to MCP-aware clients (e.g. Claude Desktop, other agents).

The MCP server is set up during the `lifespan` startup hook in `app/main.py` and shares
the same session store and query translator as the REST API.

---

## 6. Request & Response Schemas

### `SessionCreateRequest`

| Field | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| `query_type` | `"mysql" \| "postgresql" \| "sqlite" \| "sql"` | Yes | — | Dialect for SQL generation |
| `schema_context` | `list[TableSchema]` | Conditional | `null` | Required if no `db_url` |
| `db_url` | `string` | No | `null` | Enables live execution + auto-discovery |
| `system_instructions` | `string` | No | `null` | Extra LLM instructions |

### `ChatMessageRequest`

| Field | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| `message` | `string` | Yes | — | User's natural-language input |
| `mode` | `"ask" \| "visualize" \| "agent"` | No | `"ask"` | Interaction mode |
| `confirmed` | `boolean` | No | `false` | Confirm a pending AGENT action |

### `ChatMessageResponse`

| Field | Type | Always present | Notes |
|-------|------|----------------|-------|
| `role` | `"assistant"` | Yes | |
| `mode` | `ChatMode` | Yes | Echo of request mode |
| `content` | `string` | Yes | Human-readable answer |
| `query` | `string \| null` | No | Generated SQL (VISUALIZE) |
| `query_result` | `list[dict] \| null` | No | Raw rows (VISUALIZE) |
| `visualization` | `VisualizationSpec \| null` | No | Chart spec (VISUALIZE) |
| `agent_reasoning` | `list[dict] \| null` | No | ReAct steps (AGENT) |
| `tool_results` | `list[dict] \| null` | No | Tool outputs (AGENT) |
| `requires_confirmation` | `boolean` | Yes | Destructive action gate |
| `pending_action` | `PendingActionModel \| null` | No | Action awaiting confirmation |
| `sources` | `list[dict] \| null` | No | RAG citations (ASK) |
| `error` | `string \| null` | No | Error detail if any |

---

## 7. Authentication

### API Key (`X-API-Key`)

All endpoints require the `X-API-Key` header.  Set one or more keys (comma-separated)
in `API_KEYS`.  The service returns `403` for missing or invalid keys.

```bash
curl -H "X-API-Key: your-key" http://localhost:8000/sessions
```

### Employee JWT (`Authorization: Bearer`)

VISUALIZE and AGENT modes require a valid JWT issued by the CRM backend.  The JWT
must contain:

| Claim | Used For |
|-------|----------|
| `empId` | Employee identity in AGENT tool calls |
| `companyId` | Tenant scoping in VISUALIZE queries + AGENT calls |
| `role` | Passed to the AGENT system prompt |

When `JWT_SECRET` is set, the token is verified with HMAC-HS256.  When blank, the
token is decoded without signature verification (suitable for internal-only deployments
where the CRM is the only caller).

---

## 8. Rate Limiting

Default: **60 requests per minute per IP**.

Configurable via environment variables:

```
RATE_LIMIT=60/minute
```

Middleware returns `429 Too Many Requests` when the limit is exceeded.

---

## 9. Background Services

### Session TTL

Sessions expire after `SESSION_TTL_SECONDS` (default: 3 600 s / 1 hour) of inactivity.
Expiry is enforced on read — the session store returns `None` for expired sessions.

### Database Connection Warm-up

On startup, the `lifespan` handler executes `SELECT 1` in a background thread to verify
reachability and pre-fill the connection pool before the first request arrives.

### MCP Server Mount

After the DB is ready, `mount_mcp_server()` is called to attach the MCP SSE endpoint.

---

## 10. Docker

```bash
cp .env.example .env
# Edit .env

docker compose up --build
```

The `docker-compose.yml` builds the image from `Dockerfile` and mounts `.env`.

**Note:** For Aiven MySQL, do **not** mount `ca.pem` inside Docker.  Use the
`DB_SSL_CA_B64` variable (base64-encoded PEM content) instead:

```bash
DB_SSL_CA_B64=$(base64 -w0 ca.pem)
```

After containers start, run the knowledge ingest:

```bash
docker compose exec api python -m app.rag.ingest --reset
```

---

## 11. Tests

```bash
# Install test dependencies (included in requirements.txt)
pytest tests/ -v

# Run a specific test file
pytest tests/test_chat.py -v

# With coverage
pytest tests/ --cov=app --cov-report=term-missing
```

**Test modules:**

| File | Covers |
|------|--------|
| `tests/test_chat.py` | Chat endpoint integration tests |
| `tests/test_sessions.py` | Session CRUD |
| `tests/test_agent_api.py` | AGENT mode API |
| `tests/test_agent_service.py` | Agent mode logic / guardrails |
| `tests/test_sql_adapter.py` | SQL adapter read-only enforcement |
| `tests/test_json_safety.py` | JSON serialization safety |
| `tests/test_db_url_utils.py` | DB URL parsing utilities |

---

## 12. Environment Variables

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `GROQ_API_KEY` | `""` | **Yes** | Groq API key for LLM calls |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | No | Groq model ID |
| `DATABASE_URL` | `""` | **Yes** | SQLAlchemy URL (MySQL recommended) |
| `DB_SSL_CA_B64` | `""` | No | Base64-encoded CA cert for Aiven TLS |
| `API_KEYS` | `""` | **Yes** | Comma-separated API key(s) |
| `JWT_SECRET` | `""` | No | HMAC secret to verify CRM JWTs |
| `JWT_ALGORITHMS` | `HS256` | No | JWT algorithm(s), comma-separated |
| `CRM_BASE_URL` | `http://localhost:3000` | No | CRM API base URL for AGENT tools |
| `APP_ENV` | `development` | No | `development` or `production` |
| `LOG_LEVEL` | `INFO` | No | Logging verbosity |
| `SESSION_TTL_SECONDS` | `3600` | No | Session expiry (seconds) |
| `AGENT_MAX_STEPS` | `10` | No | Max ReAct tool-call iterations |
| `RATE_LIMIT` | `60/minute` | No | Rate limit (requests/window) |
| `EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | No | Local embedding model for RAG |
| `CHROMA_DIR` | `./.chroma` | No | ChromaDB persistence directory |
| `CHROMA_COLLECTION` | `crm_knowledge` | No | ChromaDB collection name |
| `KNOWLEDGE_DOCS_DIR` | `./knowledge` | No | Extra docs folder for RAG ingest |
| `RAG_TOP_K` | `5` | No | Number of chunks retrieved per ASK query |
