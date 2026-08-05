"""Chainlit web chat — second client for the same LangGraph agent and PocketBase
state (questions, answers, pending_questions, chat_history) as bot.py.

Cron scheduling and answer timeouts stay owned by bot.py; this app only reads
and writes the shared PocketBase state.
"""
import asyncio
import contextlib
import datetime as dt
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Optional

import chainlit as cl

import db
from agent import build_agent, invoke_agent, push_history, trim_history
from config import CHAINLIT_AUTH_PASSWORD, CHAINLIT_AUTH_USERNAME, TZ
from questions_shared import option_pairs

log = logging.getLogger("quizbot.chainlit")

# Chainlit signs its session JWTs with this; registering an auth callback makes
# it mandatory, so fail here with a clear message instead of deep inside the CLI.
if not os.environ.get("CHAINLIT_AUTH_SECRET"):
    raise RuntimeError(
        "CHAINLIT_AUTH_SECRET must be set to run chainlit_app.py "
        "(generate one with `chainlit create-secret`)"
    )
if not CHAINLIT_AUTH_USERNAME or not CHAINLIT_AUTH_PASSWORD:
    raise RuntimeError(
        "CHAINLIT_AUTH_USERNAME / CHAINLIT_AUTH_PASSWORD must be set to run chainlit_app.py"
    )

# Cap on how long a question's buttons stay on screen. An outstanding ask
# disables the chat input, so it must not hold the page hostage for the
# question's full timeout_minutes; the record stays pending in PocketBase
# either way, and reloading the page offers it again.
ASK_TIMEOUT_SECONDS = 300

db.init()

agent = build_agent(reschedule_question=lambda qid: None, reschedule_prompt=lambda pid: None)
agent_lock = asyncio.Lock()


@cl.password_auth_callback
async def auth_callback(username: str, password: str) -> Optional[cl.User]:
    # Compare as bytes: compare_digest rejects str with non-ASCII characters.
    user_ok = secrets.compare_digest(username.encode(), CHAINLIT_AUTH_USERNAME.encode())
    pass_ok = secrets.compare_digest(password.encode(), CHAINLIT_AUTH_PASSWORD.encode())
    if user_ok and pass_ok:
        return cl.User(identifier=username)
    return None


async def _send_reply(reply: str, images: list[str]) -> None:
    elements = [cl.Image(path=p, name=Path(p).name, display="inline") for p in images]
    if reply or elements:
        await cl.Message(content=reply, elements=elements).send()
    for p in images:
        with contextlib.suppress(OSError):
            Path(p).unlink()


async def _ask_choice_question(q: dict[str, Any], pending: dict[str, Any]) -> None:
    remaining = (dt.datetime.fromisoformat(pending["expires_at"]) - dt.datetime.now(TZ)).total_seconds()
    actions = [
        cl.Action(name="answer", payload={"label": label, "value": value}, label=label)
        for label, value in option_pairs(q)
    ]
    res = await cl.AskActionMessage(
        content=f"❓ {q['text']}",
        actions=actions,
        timeout=max(min(int(remaining), ASK_TIMEOUT_SECONDS), 1),
    ).send()
    if res is None:
        return
    current = db.get_pending_question(pending["id"])
    if not current or current["status"] != "pending":
        return  # answered from Telegram, or expired, while the buttons were up
    db.save_answer(q["id"], q["type"], res["payload"]["label"])
    db.mark_pending_status(pending["id"], "answered")


async def _prompt_next_open_question() -> None:
    queue: list[dict[str, str]] = cl.user_session.get("open_queue") or []
    if queue:
        await cl.Message(content=f"❓ {queue[0]['text']}\n\n(cevabını yazıp gönder)").send()


@cl.on_chat_start
async def on_chat_start() -> None:
    pendings: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for pending in db.list_pending_questions():
        q = db.get_question(pending["qid"])
        if q:
            pendings.append((q, pending))

    # Chainlit keeps a single ask slot per session: a second concurrent ask
    # replaces the first one's buttons in the UI. Render them one at a time.
    for q, pending in pendings:
        if q["type"] != "open":
            await _ask_choice_question(q, pending)

    # Open questions are answered as plain chat messages, so they are queued and
    # prompted one by one (only after every ask above resolved — an outstanding
    # ask disables the chat input).
    open_queue = [
        {"qid": q["id"], "pending_id": pending["id"], "text": q["text"]}
        for q, pending in pendings
        if q["type"] == "open"
    ]
    cl.user_session.set("open_queue", open_queue)
    await _prompt_next_open_question()


async def _capture_open_answer(text: str) -> None:
    queue: list[dict[str, str]] = cl.user_session.get("open_queue") or []
    entry = queue.pop(0)
    cl.user_session.set("open_queue", queue)
    pending = db.get_pending_question(entry["pending_id"])
    if pending and pending["status"] == "pending":
        db.save_answer(entry["qid"], "open", text)
        db.mark_pending_status(entry["pending_id"], "answered")
        await cl.Message(content="Kaydedildi 📝").send()
    else:
        await cl.Message(content="Bu sorunun süresi doldu, kaydedilmedi.").send()
    await _prompt_next_open_question()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    text = message.content or ""
    if not text.strip():
        return

    if cl.user_session.get("open_queue"):
        await _capture_open_answer(text)
        return

    history = trim_history()
    try:
        async with agent_lock:
            reply, images = await invoke_agent(agent, history=list(history), user_text=text)
    except Exception as exc:
        log.exception("agent invocation failed")
        await cl.Message(content=f"⚠️ Agent hata: {exc}").send()
        return
    push_history("user", text)
    push_history("assistant", reply)
    await _send_reply(reply, images)
