"""MCP (SSE) server exposing the bot's LLM agent to external agents.

Single tool: ask_agent(prompt) — invokes the same ReAct agent the Telegram
bot uses. History is kept per MCP session (one SSE connection = one session),
isolated from the Telegram chat history.

Auth: Bearer token via the MCP_TOKEN env var, checked by a pure ASGI
middleware in front of the SSE app. BaseHTTPMiddleware cannot be used here
because it buffers response bodies and breaks text/event-stream.
"""
import asyncio
import base64
import logging
import secrets
import weakref
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ImageContent, TextContent

from agent import invoke_agent, push_history, trim_history
from config import MCP_HOST, MCP_PORT, MCP_TOKEN

log = logging.getLogger("quizbot.mcp")

# Per-SSE-session history. WeakKeyDictionary: when the session object is GC'd
# (client disconnect), its entry vanishes automatically. No id() reuse risk.
_sessions: "weakref.WeakKeyDictionary[Any, dict[str, Any]]" = weakref.WeakKeyDictionary()


def _image_blocks_from_paths(paths: list[str]) -> list[ImageContent]:
    blocks: list[ImageContent] = []
    for path_str in paths:
        path = Path(path_str)
        try:
            data = path.read_bytes()
        except OSError as exc:
            log.warning("could not read chart %s: %s", path, exc)
            continue
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        blocks.append(
            ImageContent(
                type="image",
                data=base64.b64encode(data).decode("ascii"),
                mimeType="image/png",
            )
        )
    return blocks


def build_mcp(agent: Any, agent_lock: asyncio.Lock | None = None) -> FastMCP:
    mcp = FastMCP("data-collection-bot")

    @mcp.tool(
        description=(
            "Telegram botunun LLM agent'ine prompt yolla. "
            "Agent CRUD/SQL/Python tools'larını kullanarak cevap üretir. "
            "Üretilen grafikler image content olarak döner. "
            "Her MCP session ayrı history tutar (Telegram'dan bağımsız)."
        )
    )
    async def ask_agent(prompt: str, ctx: Context) -> list:
        session = ctx.request_context.session
        state = _sessions.get(session)
        if state is None:
            state = {}
            _sessions[session] = state
        history = trim_history(state)
        if agent_lock is None:
            reply, images = await invoke_agent(
                agent, history=list(history), user_text=prompt
            )
        else:
            async with agent_lock:
                reply, images = await invoke_agent(
                    agent, history=list(history), user_text=prompt
                )
        push_history(state, "user", prompt)
        push_history(state, "assistant", reply)

        blocks: list = [TextContent(type="text", text=reply or "")]
        blocks.extend(_image_blocks_from_paths(images))
        return blocks

    return mcp


class BearerAuthASGI:
    """Pure ASGI auth middleware.

    BaseHTTPMiddleware wraps the response in a buffering Send wrapper which
    blocks streaming responses like SSE. Operating at the ASGI level lets
    us pass the receive/send callables through untouched."""

    def __init__(self, app, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}".encode("latin-1")

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return
        authorized = False
        for name, value in scope.get("headers", ()):
            if name == b"authorization" and secrets.compare_digest(value, self._expected):
                authorized = True
                break
        if not authorized:
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                }
            )
            await send({"type": "http.response.body", "body": b"unauthorized"})
            return
        await self._app(scope, receive, send)


async def serve(agent: Any, agent_lock: asyncio.Lock | None = None) -> None:
    if not MCP_TOKEN:
        raise RuntimeError("MCP_TOKEN env var is required to start the MCP server.")
    mcp = build_mcp(agent, agent_lock=agent_lock)
    app = BearerAuthASGI(mcp.sse_app(), token=MCP_TOKEN)
    config = uvicorn.Config(app, host=MCP_HOST, port=MCP_PORT, log_level="info")
    server = uvicorn.Server(config)
    log.info("MCP SSE serving on http://%s:%d/sse", MCP_HOST, MCP_PORT)
    await server.serve()
