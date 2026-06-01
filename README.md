# Telegram Data-Collection Bot (LLM-powered)

[![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-21.6-26A5E4?logo=telegram&logoColor=white)](https://github.com/python-telegram-bot/python-telegram-bot)
[![LangChain](https://img.shields.io/badge/LangChain-0.3-1C3C3C?logo=langchain&logoColor=white)](https://www.langchain.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2-FF6B35)](https://langchain-ai.github.io/langgraph/)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![SQLite](https://img.shields.io/badge/SQLite-bundled-003B57?logo=sqlite&logoColor=white)](https://www.sqlite.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A Telegram bot that regularly collects data from its owner, returns statistics,
and can manage its own configuration through a LangChain agent. The "quiz"
naming is misleading: there is no right/wrong — it just records data.

## Components
- **Questions** (`questions`): each has its own cron and timeout. If no answer
  arrives in time, the prompt message is deleted.
- **Answers** (`answers`): stored in SQLite.
- **Scheduled prompts** (`scheduled_prompts`): cron-driven LLM **instructions**
  whose output is sent back to the owner. The `prompt` column must be an
  imperative task for the agent (e.g. "Summarize the last 7 days of mood data"),
  not a chat question. Typically used for recurring reports.
- **LLM agent**: replies to the owner's free-form messages, performs CRUD
  through tools, runs SQL, and manages scheduled prompts.

## Question types
- `scale` → numeric scale (e.g. 1-5 buttons)
- `rating` → labeled scale with options
- `choice` → multiple choice
- `open` → open-ended (reply to the bot's message to answer)

## 1) Create the bot
1. Open **@BotFather** in Telegram → `/newbot` → get the token.
2. Copy `.env.example` to `.env`, fill `BOT_TOKEN`.
3. Leave `CHAT_ID` empty for now.
4. Run the bot, send `/start` to it from your own account → it will reply with
   your chat id.
5. Put that id into `.env` as `CHAT_ID`, add the LLM provider + key, then
   **restart** the bot.

While `CHAT_ID` is empty the bot only echoes the caller's chat id and does
nothing else — the LLM and the scheduler stay disabled. This keeps the bot
inert until you have finished setup.

## 2) Local run
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)
python bot.py
```

## 3) Docker
```bash
docker compose up -d --build
```
The named volume `quizbot_data` mounted at `/data` holds `answers.db`.
The container runs as a non-root user inside the image.

On first boot, if the DB is empty, `questions.json` is loaded as a seed.
After that, the DB is the canonical source.

## 4) LLM agent
Any plain-text message from the owner triggers the agent. Tools:

| Tool | Purpose |
|---|---|
| `list_questions` / `get_question` | read |
| `add_question` / `update_question` / `delete_question` | question CRUD |
| `query_answers` / `delete_answer` | filter and remove answers |
| `list_scheduled_prompts` / `add_scheduled_prompt` / `update_scheduled_prompt` / `delete_scheduled_prompt` | scheduled prompt CRUD |
| `run_sql` | raw SQL (single statement) |
| `run_python` | Python execution for analytics and chart generation (`pd`, `plt`, `db` available) |
| `now` | current date-time in the bot's timezone |

When CRUD tools change the DB the scheduler auto-resyncs (no restart needed).

Chat memory: keeps the last 20 turns, and resets if more than 1 hour has
passed since the previous message. Use `/clear` to reset immediately. MCP
sessions use the same limit/reset rules (Telegram `/clear` does not affect MCP).

### LLM providers
`LLM_PROVIDER` env: `openai` (default) | `anthropic` | `ollama`.
Provider key env: `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `OLLAMA_BASE_URL`.
Override the model with `LLM_MODEL`.

Default models per provider:
- `openai` → `gpt-4o-mini`
- `anthropic` → `claude-haiku-4-5-20251001`
- `ollama` → `llama3.1`

## 5) Commands
- `/start` — chat id + help
- `/list` — active questions + scheduled prompts + next cron run
- `/ask [qid]` — trigger manually (no qid → all)
- `/stats` — summary plus a scale-trend chart
- `/clear` — reset Telegram LLM chat history (next message starts a fresh session)

All commands respond only to the configured `CHAT_ID`; other senders are
ignored silently.

## 6) Configuration

| Env var | Default | Notes |
|---|---|---|
| `BOT_TOKEN` | — | required |
| `CHAT_ID` | empty | owner-only filter; empty = id-only mode |
| `TIMEZONE` | `Europe/Istanbul` | IANA tz used for cron + display |
| `DB_PATH` | `./answers.db` | SQLite file path |
| `LLM_PROVIDER` | `openai` | `openai` / `anthropic` / `ollama` |
| `LLM_MODEL` | provider default | override |
| `OPENAI_API_KEY` | — | required when provider = openai |
| `ANTHROPIC_API_KEY` | — | required when provider = anthropic |
| `OLLAMA_BASE_URL` | — | e.g. `http://localhost:11434` |
| `MCP_HOST` | `127.0.0.1` | bind host for the MCP SSE server |
| `MCP_PORT` | `8765` | bind port for the MCP SSE server |
| `MCP_TOKEN` | empty | Bearer token; empty disables the MCP server |

## 7) MCP (SSE) server
When `MCP_TOKEN` is set, the bot also exposes its agent as an MCP server over
SSE so other agents (Claude Code, IDE clients, …) can call it.

- Endpoint: `http://$MCP_HOST:$MCP_PORT/sse`
- Auth: `Authorization: Bearer $MCP_TOKEN` on every request
- Tool: `ask_agent(prompt: str)` — runs the same ReAct agent the Telegram
  bot uses. Returns text plus any matplotlib charts as image content.
- History: each SSE connection is its own session with its own history,
  separate from the Telegram chat history. The same turn/gap limits are applied.
- Concurrency: Telegram, scheduled prompts, and MCP calls share one agent
  instance; agent invocations are serialized with an async lock to avoid
  overlapping tool/SQLite execution.

Example client config (Claude Code):
```json
{
  "mcpServers": {
    "quizbot": {
      "transport": "sse",
      "url": "http://127.0.0.1:8765/sse",
      "headers": { "Authorization": "Bearer YOUR_MCP_TOKEN" }
    }
  }
}
```

## 8) Schema (manual SQL access)
```sql
CREATE TABLE questions (
  id TEXT PK, type TEXT, text TEXT,
  config TEXT,        -- JSON
  cron TEXT, timeout_minutes INTEGER, active INTEGER
);
CREATE TABLE answers (
  id INTEGER PK AUTOINCREMENT,
  ts TEXT, day TEXT, qid TEXT, qtype TEXT, answer TEXT
);
CREATE TABLE scheduled_prompts (
  id TEXT PK, prompt TEXT, cron TEXT, active INTEGER
);
```
`config` examples:
- `scale`: `{"min":1,"max":5,"labels":{"1":"...","5":"..."}}`
- `rating`/`choice`: `{"options":["a","b","c"]}`
- `open`: `{}`

If you mutate the DB through direct SQL, restart the bot so the scheduler
picks up the change (agent tools auto-resync; raw SQL does not).

## 9) Backup
```bash
0 3 * * * cp data/answers.db backups/answers_$(date +\%F).db
```

## Notes
- Open questions must be answered as a **reply** to the bot's prompt
  (ForceReply is set automatically). Plain text without a reply goes to the
  LLM agent.
- Cron jobs missed while the bot was offline are not back-filled.
- The `run_sql` tool is not sandboxed — DROP/TRUNCATE/DELETE are possible
  despite warnings in the agent prompt. Acceptable for a single-user
  personal bot.
- The `run_python` tool executes arbitrary Python code and is also not
  sandboxed. Keep the bot owner-only and do not expose MCP publicly.
