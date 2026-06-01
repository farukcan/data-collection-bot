"""Environment-driven configuration. Imported by all other modules."""
import os
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

BOT_TOKEN: str = os.environ["BOT_TOKEN"]

_chat = os.environ.get("CHAT_ID", "").strip()
# When CHAT_ID is unset the bot runs in id-only mode: it replies to anyone
# with their own chat_id and refuses everything else.
CHAT_ID: Optional[int] = int(_chat) if _chat else None

TIMEZONE: str = os.environ.get("TIMEZONE", "Europe/Istanbul")
TZ: ZoneInfo = ZoneInfo(TIMEZONE)

DB: Path = Path(os.environ.get("DB_PATH", str(Path(__file__).parent.resolve() / "answers.db")))
SEED_FILE: Path = Path(__file__).parent.resolve() / "questions.json"

LLM_PROVIDER: str = os.environ.get("LLM_PROVIDER", "openai")
_MODEL_DEFAULTS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5-20251001",
    "ollama": "llama3.1",
}
LLM_MODEL: str = os.environ.get("LLM_MODEL", _MODEL_DEFAULTS.get(LLM_PROVIDER, "gpt-4o-mini"))

# How long a chat history slot stays valid before being reset (seconds).
HISTORY_GAP_SECONDS: int = 3600
HISTORY_MAX_TURNS: int = 20

# MCP (SSE) server — exposes the LLM agent to external agents.
MCP_HOST: str = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT: int = int(os.environ.get("MCP_PORT", "8765"))
MCP_TOKEN: str = os.environ.get("MCP_TOKEN", "")
