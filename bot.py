#!/usr/bin/env python3
"""Telegram data-collection bot with per-question cron, timeouts, and an LLM
agent that can manage questions, answers, and scheduled prompts."""
import asyncio
import contextlib
import datetime as dt
import json
import logging
import tempfile
from pathlib import Path
from typing import Any, Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from croniter import croniter
from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import os

import db
import tables
from agent import (
    build_agent,
    clear_history,
    invoke_agent,
    push_history,
    scheduled_prompt_invocation_text,
    trim_history,
)
from config import BOT_TOKEN, CHAT_ID, MCP_TOKEN, TIMEZONE, TZ
from mcp_server import serve as serve_mcp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("quizbot")


# ---------- question delivery ----------
def build_keyboard(q: dict[str, Any]) -> Any:
    qid = q["id"]
    cfg = q["config"]
    qtype = q["type"]
    if qtype == "scale":
        btns = [
            InlineKeyboardButton(str(v), callback_data=f"{qid}|{v}")
            for v in range(cfg["min"], cfg["max"] + 1)
        ]
        row_size = 5 if len(btns) > 5 else len(btns)
        rows = [btns[i : i + row_size] for i in range(0, len(btns), row_size)]
        return InlineKeyboardMarkup(rows)
    if qtype in ("rating", "choice"):
        rows = [
            [InlineKeyboardButton(opt, callback_data=f"{qid}|{i}")]
            for i, opt in enumerate(cfg["options"])
        ]
        return InlineKeyboardMarkup(rows)
    if qtype == "open":
        return ForceReply(selective=False)
    raise ValueError(f"Unknown question type: {qtype}")


async def send_question(context: ContextTypes.DEFAULT_TYPE, qid: str) -> None:
    q = db.get_question(qid)
    if not q or not q["active"]:
        log.warning("send_question: %s not active", qid)
        return
    cfg = q["config"]
    text = f"❓ {q['text']}"
    if q["type"] == "scale" and "labels" in cfg:
        hints = ", ".join(f"{k}={v}" for k, v in cfg["labels"].items())
        text += f"\n({hints})"
    elif q["type"] == "open":
        text += "\n\n(cevabını yazıp gönder)"

    msg = await context.bot.send_message(CHAT_ID, text, reply_markup=build_keyboard(q))

    if q["type"] == "open":
        pending = context.bot_data.setdefault("pending_open", {})
        pending[msg.message_id] = qid

    context.job_queue.run_once(
        delete_message_job,
        when=q["timeout_minutes"] * 60,
        data={"chat_id": CHAT_ID, "message_id": msg.message_id, "qid": qid},
        name=f"timeout:{qid}:{msg.message_id}",
    )


async def delete_message_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    try:
        await context.bot.delete_message(data["chat_id"], data["message_id"])
    except Exception as exc:
        log.info("delete_message_job failed mid=%s: %s", data["message_id"], exc)
    context.bot_data.get("pending_open", {}).pop(data["message_id"], None)


def cancel_timeout(context: ContextTypes.DEFAULT_TYPE, qid: str, message_id: int) -> None:
    for job in context.job_queue.get_jobs_by_name(f"timeout:{qid}:{message_id}"):
        job.schedule_removal()


# ---------- scheduling: questions ----------
async def question_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    qid: str = context.job.data["qid"]
    try:
        await send_question(context, qid)
    except Exception:
        log.exception("send_question failed for %s", qid)
    finally:
        schedule_question(context.job_queue, qid)


def schedule_question(job_queue, qid: str) -> None:
    """Cancel any existing schedule for qid, then schedule the next firing if active."""
    for job in job_queue.get_jobs_by_name(f"cron:q:{qid}"):
        job.schedule_removal()
    q = db.get_question(qid)
    if not q or not q["active"]:
        return
    next_fire = croniter(q["cron"], dt.datetime.now(TZ)).get_next(dt.datetime)
    job_queue.run_once(
        question_job,
        when=next_fire,
        data={"qid": qid},
        name=f"cron:q:{qid}",
    )
    log.info("Scheduled question %s at %s", qid, next_fire.isoformat())


# ---------- scheduling: prompts ----------
async def scheduled_prompt_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    pid: str = context.job.data["pid"]
    p = db.get_scheduled_prompt(pid)
    if not p or not p["active"]:
        return
    agent = context.bot_data["agent"]
    agent_lock: asyncio.Lock = context.bot_data["agent_lock"]
    try:
        async with agent_lock:
            reply, images = await invoke_agent(
                agent,
                history=[],
                user_text=scheduled_prompt_invocation_text(p["prompt"]),
            )
    except Exception as exc:
        log.exception("scheduled_prompt %s failed", pid)
        await context.bot.send_message(CHAT_ID, f"⚠️ Scheduled prompt {pid} hata: {exc}")
        schedule_prompt(context.job_queue, pid)
        return
    await send_images(context.bot, CHAT_ID, images)
    if reply:
        await context.bot.send_message(CHAT_ID, f"⏰ {pid}\n\n{reply}")
    schedule_prompt(context.job_queue, pid)


async def send_images(bot, chat_id: int, paths: list[str]) -> None:
    for path in paths:
        try:
            with open(path, "rb") as fp:
                await bot.send_photo(chat_id, fp)
        except Exception:
            log.exception("send_photo failed for %s", path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


def schedule_prompt(job_queue, pid: str) -> None:
    for job in job_queue.get_jobs_by_name(f"cron:p:{pid}"):
        job.schedule_removal()
    p = db.get_scheduled_prompt(pid)
    if not p or not p["active"]:
        return
    next_fire = croniter(p["cron"], dt.datetime.now(TZ)).get_next(dt.datetime)
    job_queue.run_once(
        scheduled_prompt_job,
        when=next_fire,
        data={"pid": pid},
        name=f"cron:p:{pid}",
    )
    log.info("Scheduled prompt %s at %s", pid, next_fire.isoformat())


def schedule_all(job_queue) -> None:
    for q in db.list_questions(active_only=True):
        schedule_question(job_queue, q["id"])
    for p in db.list_scheduled_prompts(active_only=True):
        schedule_prompt(job_queue, p["id"])


# ---------- command handlers ----------
async def reply_table(
    message,
    headers: list[str],
    rows: list[list[str]],
    *,
    caption: str | None = None,
    path_prefix: str = "table",
) -> None:
    path = Path("/tmp") / f"{path_prefix}_{message.message_id}.png"
    try:
        tables.render_table_from_rows(path, headers, rows)
        with path.open("rb") as fp:
            await message.reply_photo(fp, caption=caption)
    except Exception:
        log.exception("reply_table failed prefix=%s", path_prefix)
        await message.reply_text("Tablo görseli oluşturulamadı.")
    finally:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await reply_table(
        update.message,
        ["Command", "Description"],
        [
            ["/ask [qid]", "Soru(ları) şimdi gönder"],
            ["/list", "Aktif sorular ve scheduled prompts"],
            ["/stats", "İstatistikler"],
            ["/dump", "Tüm veritabanını JSON olarak indir"],
            ["/clear", "LLM sohbet geçmişini sıfırla"],
            ["(metin)", "LLM agent: CRUD, sorgu, SQL, scheduled prompts"],
        ],
        caption=f"Chat ID: {chat_id}",
        path_prefix="start",
    )


async def cmd_start_idonly(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Used when CHAT_ID env is unset: only reveals chat_id, nothing else."""
    await update.message.reply_text(
        f"Chat ID: {update.effective_chat.id}\n\n"
        "Bu botun sahibi henüz CHAT_ID env'ini ayarlamamış. "
        "Sahibiysen bu ID'yi env'e koy ve botu yeniden başlat."
    )


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        qid = context.args[0]
        if not db.get_question(qid):
            await update.message.reply_text(f"{qid} bulunamadı.")
            return
        await send_question(context, qid)
        return
    questions = db.list_questions(active_only=True)
    if not questions:
        await update.message.reply_text("Aktif soru yok.")
        return
    for q in questions:
        await send_question(context, q["id"])


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _list_schedule_rows(
    questions: list[dict[str, Any]],
    prompts: list[dict[str, Any]],
) -> list[list[str]]:
    now = dt.datetime.now(TZ)
    rows: list[list[str]] = []
    for q in questions:
        nxt = croniter(q["cron"], now).get_next(dt.datetime)
        rows.append(
            [
                "Question",
                q["id"],
                q["type"],
                _truncate(q["text"], 80),
                q["cron"],
                nxt.strftime("%Y-%m-%d %H:%M"),
                f"{q['timeout_minutes']}m",
            ]
        )
    for p in prompts:
        nxt = croniter(p["cron"], now).get_next(dt.datetime)
        rows.append(
            [
                "Prompt",
                p["id"],
                "-",
                _truncate(p["prompt"], 60),
                p["cron"],
                nxt.strftime("%Y-%m-%d %H:%M"),
                "-",
            ]
        )
    return rows


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    questions = db.list_questions(active_only=True)
    prompts = db.list_scheduled_prompts(active_only=True)
    if not questions and not prompts:
        await update.message.reply_text("Aktif soru veya scheduled prompt yok.")
        return
    await reply_table(
        update.message,
        ["Kind", "ID", "Type", "Text", "Cron", "Next", "Timeout"],
        _list_schedule_rows(questions, prompts),
        caption="Aktif sorular ve scheduled prompts",
        path_prefix="list",
    )


async def cmd_dump(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = db.dump_all()
    payload = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    try:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp.write(payload.encode("utf-8"))
            tmp_path = Path(tmp.name)
        with tmp_path.open("rb") as fp:
            await update.message.reply_document(fp, filename="db_dump.json")
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_history(context.bot_data)
    await update.message.reply_text(
        "LLM sohbet geçmişi sıfırlandı. Sonraki mesajlar önceki konuşmayı görmez."
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    df = db.to_df("answers")
    if df.empty:
        await update.message.reply_text("Henüz veri yok.")
        return
    scales = df[df.qtype == "scale"].copy()
    if not scales.empty:
        scales["num"] = pd.to_numeric(scales.answer, errors="coerce")
        scale_rows = [
            [qid, f"{g.num.mean():.1f}", str(len(g))]
            for qid, g in scales.groupby("qid")
        ]
        scale_rows.append(["Toplam kayıt", str(len(df)), ""])
        scale_rows.append(["Gün sayısı", str(df.day.nunique()), ""])
        await reply_table(
            update.message,
            ["Question", "Average", "n"],
            scale_rows,
            caption="İstatistikler",
            path_prefix="stats",
        )
    else:
        await reply_table(
            update.message,
            ["Metric", "Value"],
            [
                ["Toplam kayıt", str(len(df))],
                ["Gün sayısı", str(df.day.nunique())],
            ],
            caption="İstatistikler",
            path_prefix="stats",
        )

    if not scales.empty:
        fig, ax = plt.subplots(figsize=(8, 4))
        for qid, g in scales.groupby("qid"):
            daily = g.groupby("day")["num"].mean()
            ax.plot(daily.index, daily.values, marker="o", label=qid)
        ax.set_title("Ölçek soruları - günlük ortalama")
        ax.legend()
        ax.grid(alpha=0.3)
        plt.xticks(rotation=45)
        plt.tight_layout()
        path = Path("/tmp") / "trend.png"
        fig.savefig(path, dpi=110)
        plt.close(fig)
        with path.open("rb") as fp:
            await update.message.reply_photo(fp)


# ---------- answer capture ----------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        qid, val = query.data.split("|", 1)
    except ValueError:
        log.warning("on_button: malformed callback_data %r", query.data)
        return
    q = db.get_question(qid)
    if not q:
        await query.edit_message_text("Soru artık mevcut değil.")
        return
    cfg = q["config"]
    answer_label = val
    if q["type"] in ("rating", "choice"):
        idx = int(val)
        if idx < 0 or idx >= len(cfg["options"]):
            return
        answer_label = cfg["options"][idx]
    db.save_answer(qid, q["type"], answer_label)
    await query.edit_message_text(f"❓ {q['text']}\n→ Kaydedildi: {answer_label}")
    cancel_timeout(context, qid, query.message.message_id)


async def on_open_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[bool]:
    """Capture a text that's a reply to a pending open question.
    Returns True if consumed so the chat handler can skip it."""
    msg = update.message
    if not msg.reply_to_message:
        return None
    pending: dict[int, str] = context.bot_data.get("pending_open", {})
    qid = pending.get(msg.reply_to_message.message_id)
    if not qid:
        return None
    q = db.get_question(qid)
    if not q or q["type"] != "open":
        return None
    db.save_answer(qid, "open", msg.text)
    pending.pop(msg.reply_to_message.message_id, None)
    cancel_timeout(context, qid, msg.reply_to_message.message_id)
    await msg.reply_text("Kaydedildi 📝")
    return True


# ---------- LLM chat ----------
async def on_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner free-text (not a reply to an open question) → invoke LLM agent."""
    if await on_open_reply(update, context):
        return
    msg = update.message
    text = msg.text or ""
    if not text.strip():
        return
    agent = context.bot_data["agent"]
    agent_lock: asyncio.Lock = context.bot_data["agent_lock"]
    history = trim_history(context.bot_data)
    await context.bot.send_chat_action(msg.chat_id, "typing")
    try:
        async with agent_lock:
            reply, images = await invoke_agent(agent, history=list(history), user_text=text)
    except Exception as exc:
        log.exception("agent invocation failed")
        await msg.reply_text(f"⚠️ Agent hata: {exc}")
        return
    push_history(context.bot_data, "user", text)
    push_history(context.bot_data, "assistant", reply)
    await send_images(context.bot, msg.chat_id, images)
    if reply:
        await msg.reply_text(reply)


# ---------- main ----------
def main() -> None:
    db.init()

    if CHAT_ID is None:
        log.warning("CHAT_ID unset — running in id-only mode (no LLM, no scheduler).")
        app = Application.builder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", cmd_start_idonly))
        app.add_handler(MessageHandler(filters.ALL, cmd_start_idonly))
        log.info("Bot starting in id-only mode (tz=%s)", TIMEZONE)
        app.run_polling()
        return

    seeded = db.seed_if_empty()
    if seeded:
        log.info("Seeded %d questions from questions.json", seeded)

    def _on_mcp_task_done(task: asyncio.Task) -> None:
        if task.cancelled():
            log.info("MCP SSE server task cancelled")
            return
        exc = task.exception()
        if exc:
            log.exception("MCP SSE server task failed", exc_info=exc)
            return
        log.info("MCP SSE server task exited cleanly")

    async def post_init(application: Application) -> None:
        if not MCP_TOKEN:
            log.info("MCP_TOKEN unset — MCP SSE server not started")
            return
        # Hold the task reference on bot_data so the GC can't collect it
        # mid-flight and silently swallow uvicorn errors.
        mcp_task = asyncio.create_task(
            serve_mcp(
                application.bot_data["agent"],
                agent_lock=application.bot_data["agent_lock"],
            ),
            name="mcp-sse-server",
        )
        mcp_task.add_done_callback(_on_mcp_task_done)
        application.bot_data["mcp_task"] = mcp_task
        log.info("MCP SSE server task scheduled")

    async def post_shutdown(application: Application) -> None:
        mcp_task: asyncio.Task | None = application.bot_data.get("mcp_task")
        if not mcp_task:
            return
        if not mcp_task.done():
            mcp_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await mcp_task

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Tool callbacks need the job_queue from the running Application.
    job_queue = app.job_queue
    agent = build_agent(
        reschedule_question=lambda qid: schedule_question(job_queue, qid),
        reschedule_prompt=lambda pid: schedule_prompt(job_queue, pid),
    )
    app.bot_data["agent"] = agent
    app.bot_data["agent_lock"] = asyncio.Lock()

    owner = filters.Chat(CHAT_ID)
    app.add_handler(CommandHandler("start", cmd_start, filters=owner))
    app.add_handler(CommandHandler("ask", cmd_ask, filters=owner))
    app.add_handler(CommandHandler("list", cmd_list, filters=owner))
    app.add_handler(CommandHandler("stats", cmd_stats, filters=owner))
    app.add_handler(CommandHandler("dump", cmd_dump, filters=owner))
    app.add_handler(CommandHandler("clear", cmd_clear, filters=owner))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & owner,
            on_chat,
        )
    )

    schedule_all(job_queue)
    log.info("Bot starting (tz=%s, owner=%s)", TIMEZONE, CHAT_ID)
    app.run_polling()


if __name__ == "__main__":
    main()
