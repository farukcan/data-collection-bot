#!/usr/bin/env python3
"""Telegram data-collection bot with per-question cron, timeouts, and an LLM
agent that can manage questions, answers, and scheduled prompts."""
import asyncio
import datetime as dt
import logging
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
from agent import build_agent, invoke_agent, push_history, trim_history
from config import BOT_TOKEN, CHAT_ID, DB, MCP_TOKEN, TIMEZONE, TZ
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
    try:
        reply, images = await invoke_agent(agent, history=[], user_text=p["prompt"])
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
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"Chat ID: {update.effective_chat.id}\n\n"
        "/ask [qid] → soru(ları) şimdi gönder\n"
        "/list → aktif soruları listele\n"
        "/stats → istatistikler\n\n"
        "Düz metin yazarsan LLM agent cevaplar (CRUD, sorgu, SQL, scheduled prompts)."
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


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    questions = db.list_questions(active_only=True)
    prompts = db.list_scheduled_prompts(active_only=True)
    if not questions and not prompts:
        await update.message.reply_text("Aktif soru veya scheduled prompt yok.")
        return
    parts: list[str] = []
    if questions:
        parts.append("📋 Sorular:")
        for q in questions:
            nxt = croniter(q["cron"], dt.datetime.now(TZ)).get_next(dt.datetime)
            parts.append(
                f"• {q['id']} ({q['type']})\n"
                f"  {q['text']}\n"
                f"  cron: {q['cron']} → {nxt:%Y-%m-%d %H:%M} | timeout: {q['timeout_minutes']}dk"
            )
    if prompts:
        parts.append("\n⏰ Scheduled prompts:")
        for p in prompts:
            nxt = croniter(p["cron"], dt.datetime.now(TZ)).get_next(dt.datetime)
            preview = p["prompt"][:60] + ("…" if len(p["prompt"]) > 60 else "")
            parts.append(f"• {p['id']} | {p['cron']} → {nxt:%Y-%m-%d %H:%M}\n  {preview}")
    await update.message.reply_text("\n".join(parts))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with db.connect() as con:
        df = pd.read_sql("SELECT * FROM answers", con)
    if df.empty:
        await update.message.reply_text("Henüz veri yok.")
        return
    lines = ["📊 İstatistikler\n"]
    scales = df[df.qtype == "scale"].copy()
    if not scales.empty:
        scales["num"] = pd.to_numeric(scales.answer, errors="coerce")
        for qid, g in scales.groupby("qid"):
            lines.append(f"{qid} ortalama: {g.num.mean():.1f}  (n={len(g)})")
    lines.append(f"\nToplam kayıt: {len(df)} | Gün sayısı: {df.day.nunique()}")
    await update.message.reply_text("\n".join(lines))

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
    history = trim_history(context.bot_data)
    await context.bot.send_chat_action(msg.chat_id, "typing")
    try:
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

    async def post_init(application: Application) -> None:
        if not MCP_TOKEN:
            log.info("MCP_TOKEN unset — MCP SSE server not started")
            return
        # Hold the task reference on bot_data so the GC can't collect it
        # mid-flight and silently swallow uvicorn errors.
        application.bot_data["mcp_task"] = asyncio.create_task(
            serve_mcp(application.bot_data["agent"]),
            name="mcp-sse-server",
        )
        log.info("MCP SSE server task scheduled")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Tool callbacks need the job_queue from the running Application.
    job_queue = app.job_queue
    agent = build_agent(
        reschedule_question=lambda qid: schedule_question(job_queue, qid),
        reschedule_prompt=lambda pid: schedule_prompt(job_queue, pid),
    )
    app.bot_data["agent"] = agent

    owner = filters.Chat(CHAT_ID)
    app.add_handler(CommandHandler("start", cmd_start, filters=owner))
    app.add_handler(CommandHandler("ask", cmd_ask, filters=owner))
    app.add_handler(CommandHandler("list", cmd_list, filters=owner))
    app.add_handler(CommandHandler("stats", cmd_stats, filters=owner))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & owner,
            on_chat,
        )
    )

    schedule_all(job_queue)
    log.info("Bot starting (tz=%s, db=%s, owner=%s)", TIMEZONE, DB, CHAT_ID)
    app.run_polling()


if __name__ == "__main__":
    main()
