"""LangChain ReAct agent with tools for questions, answers, scheduled prompts, raw SQL, and Python execution."""
import contextlib
import contextvars
import datetime as dt
import io
import json
import logging
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from langchain.chat_models import init_chat_model
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

import db
import tables
from config import HISTORY_GAP_SECONDS, HISTORY_MAX_TURNS, LLM_MODEL, LLM_PROVIDER, TZ

# Per-invocation chart buffer. invoke_agent sets a fresh list per call so
# concurrent Telegram + MCP invocations don't cross-contaminate. Drained
# inside invoke_agent and returned alongside the reply.
_pending_images: contextvars.ContextVar[list[str]] = contextvars.ContextVar(
    "pending_images"
)

log = logging.getLogger("quizbot.agent")

SCHEDULED_PROMPT_INVOCATION_HEADER = (
    "Zamanlanmış görev talimatı (bunu uygula, sonucu bot sahibine raporla):\n"
)


def scheduled_prompt_invocation_text(instruction: str) -> str:
    """Wrap a stored scheduled-prompt instruction for cron-time agent invocation."""
    return SCHEDULED_PROMPT_INVOCATION_HEADER + instruction.strip()


SYSTEM_PROMPT = """Sen bir veri toplama (self-tracking) Telegram botunun beynisin.
Bot sahibi periyodik sorularla mood, uyku, egzersiz gibi günlük veriler topluyor.
Doğru/yanlış kavramı YOK — sadece veri topluyoruz.

Yapabildiklerin:
- Sorular (questions) üzerinde CRUD
- Cevaplar (answers) üzerinde sorgu/filtreleme/silme
- Zamanlanmış prompt görevleri (scheduled_prompts) üzerinde CRUD
- Gerektiğinde ham SQL çalıştır (run_sql)
- Python kodu çalıştır (run_python): hesaplama, veri işleme ve grafik üretimi için
  ana aracın. pandas (pd), matplotlib (plt) ve db modülü hazır. Her türlü
  ortalama/medyan/yüzde/trend/korelasyon vs. işini Python'da yap; sonuçları
  print() ile bastır — stdout sana geri döner. Grafik istenirse plt figürleri
  otomatik kullanıcıya gönderilir. Tablo veya çok sütunlu liste göstermek için
  run_python içinde send_table(headers, rows) kullan; metin tablo yazma.

Kural:
- Basit listeleme/filtreleme → query_answers, list_questions vs.
- Hesaplama, aggregation, istatistik → run_python (print ile)
- Tablo / sütunlu liste gösterme → run_python + send_table(headers, rows)
- Grafik → run_python + plt
- Sadece SQL'le çözülecek özel durumlar → run_sql

Şema:
- questions(id, type, text, config, cron, timeout_minutes, active)
  type ∈ {scale, rating, choice, open}
  config (JSON):
    scale  → {"min": int, "max": int, "labels": {"<n>": "..."}}
    rating → {"options": ["a","b",...]}
    choice → {"options": ["a","b",...]}
    open   → {}
  cron 5-alanlı: "m h dom mon dow"
- answers(id, ts, day, qid, qtype, answer)
- scheduled_prompts(id, prompt, cron, active)
  prompt: her zaman LLM'e verilecek bir TALİMAT (emir kipi, yapılacak iş).
  Soru veya sohbet cümlesi değil. Örnek: "Son 7 günün mood ortalamasını
  hesapla ve kısa özet yaz." add_scheduled_prompt / update (prompt) ile
  eklerken metni bu formatta yaz.

Türkçe yanıt ver, kısa ol. Belirsizlikte sor. DROP/DELETE-WHERE'siz gibi
tehlikeli SQL'de önce onay iste. Tool sonuçlarını yorumla, ham JSON dökme.

ÖNEMLİ: Yanıtlarında markdown veya HTML formatlama kullanma. Sadece düz
metin yaz. Backtick, yıldız, alt çizgi, köşeli parantez yok. Listeler için
satır başına "• " ya da rakam yeterli.
"""


# ---------- conversation history ----------
def trim_history(bot_data: dict[str, Any]) -> list[dict[str, str]]:
    """Returns the live history list; clears it if last touch was >1h ago."""
    last_ts: Optional[dt.datetime] = bot_data.get("history_last_ts")
    now = dt.datetime.now(TZ)
    if last_ts and (now - last_ts).total_seconds() > HISTORY_GAP_SECONDS:
        bot_data["history"] = []
    return bot_data.setdefault("history", [])


def push_history(bot_data: dict[str, Any], role: str, content: str) -> None:
    history = bot_data.setdefault("history", [])
    history.append({"role": role, "content": content})
    if len(history) > HISTORY_MAX_TURNS:
        del history[: len(history) - HISTORY_MAX_TURNS]
    bot_data["history_last_ts"] = dt.datetime.now(TZ)


def clear_history(bot_data: dict[str, Any]) -> None:
    """Drop Telegram LLM chat history so the next message starts a fresh session."""
    bot_data["history"] = []
    bot_data.pop("history_last_ts", None)


def _queue_table_image(headers: list[str], rows: list[list[str]]) -> str:
    """Render a Plotly table PNG and queue it for the current agent invocation."""
    buf = _pending_images.get(None)
    if buf is None:
        log.warning("send_table outside invoke_agent scope; dropping")
        return "ERROR: send_table only works during agent invocation"
    path = Path(tempfile.gettempdir()) / f"table_{uuid.uuid4().hex}.png"
    try:
        tables.render_table_from_rows(path, headers, [list(r) for r in rows])
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"
    buf.append(str(path))
    return f"[tablo görseli kullanıcıya gönderildi ({len(rows)} satır)]"


# ---------- tools ----------
def build_tools(
    reschedule_question: Callable[[str], None],
    reschedule_prompt: Callable[[str], None],
) -> list:
    """Build tools that capture scheduler callbacks for question/prompt cron sync."""

    @tool
    def list_questions(active_only: bool) -> str:
        """Listele tüm soruları. active_only=true ise yalnız aktifler."""
        return json.dumps(db.list_questions(active_only=active_only), ensure_ascii=False, default=str)

    @tool
    def get_question(qid: str) -> str:
        """Tek soruyu id ile getir."""
        q = db.get_question(qid)
        return json.dumps(q, ensure_ascii=False, default=str) if q else "null"

    @tool
    def add_question(
        qid: str,
        qtype: str,
        text: str,
        config_json: str,
        cron: str,
        timeout_minutes: int,
    ) -> str:
        """Yeni soru ekle.
        qtype: scale|rating|choice|open
        config_json: tipe uygun JSON string (örn. '{"min":1,"max":5}')
        cron: '0 9 * * *' gibi 5 alanlı
        """
        cfg = json.loads(config_json)
        db.insert_question(qid, qtype, text, cfg, cron, timeout_minutes, active=1)
        reschedule_question(qid)
        return f"added {qid}"

    @tool
    def update_question(qid: str, fields_json: str) -> str:
        """Soru alanlarını güncelle. fields_json: {"cron":"...","timeout_minutes":30,...}"""
        fields = json.loads(fields_json)
        n = db.update_question(qid, fields)
        if n == 0:
            return f"no question with id={qid}"
        reschedule_question(qid)
        return f"updated {qid}"

    @tool
    def delete_question(qid: str) -> str:
        """Soruyu kalıcı sil. Cevaplar tabloda kalır."""
        n = db.delete_question(qid)
        reschedule_question(qid)
        return f"deleted {n}"

    @tool
    def query_answers(
        qid: Optional[str],
        since_day: Optional[str],
        until_day: Optional[str],
        since_ts: Optional[str],
        until_ts: Optional[str],
        limit: Optional[int],
    ) -> str:
        """Cevapları filtrele.
        since_day/until_day: 'YYYY-MM-DD' (gün hassasiyet).
        since_ts/until_ts: ISO 8601 saniye hassasiyet, örn '2026-06-01T15:00:00+03:00'.
        Dakika/saat filtresi için since_ts/until_ts kullan."""
        rows = db.query_answers(qid, since_day, until_day, since_ts, until_ts, limit)
        return json.dumps(rows, ensure_ascii=False, default=str)

    @tool
    def delete_answer(answer_id: int) -> str:
        """Tek cevabı id ile sil."""
        return f"deleted {db.delete_answer(answer_id)}"

    @tool
    def list_scheduled_prompts(active_only: bool) -> str:
        """Zamanlanmış prompt görevlerini listele."""
        return json.dumps(db.list_scheduled_prompts(active_only), ensure_ascii=False, default=str)

    @tool
    def add_scheduled_prompt(pid: str, prompt: str, cron: str) -> str:
        """Yeni zamanlanmış prompt görevi ekle. Cron tickte LLM talimatı uygular.

        prompt: LLM'e verilecek talimat metni (emir kipi). Soru değil.
        Örnek: "Son 7 günün uyku ve mood verilerini özetle."
        """
        db.insert_scheduled_prompt(pid, prompt, cron, active=1)
        reschedule_prompt(pid)
        return f"added {pid}"

    @tool
    def update_scheduled_prompt(pid: str, fields_json: str) -> str:
        """Zamanlanmış prompt'u güncelle. fields_json: {"prompt":"...","cron":"...","active":1}
        prompt alanı değişirse yeni metin de talimat (emir kipi) olmalı."""
        fields = json.loads(fields_json)
        n = db.update_scheduled_prompt(pid, fields)
        if n == 0:
            return f"no scheduled_prompt with id={pid}"
        reschedule_prompt(pid)
        return f"updated {pid}"

    @tool
    def delete_scheduled_prompt(pid: str) -> str:
        """Zamanlanmış prompt'u sil."""
        n = db.delete_scheduled_prompt(pid)
        reschedule_prompt(pid)
        return f"deleted {n}"

    @tool
    def run_sql(query: str) -> str:
        """Tek deyimlik ham SQL çalıştır. Tablolar: questions, answers, scheduled_prompts.
        SELECT için satırlar, write için rowcount döner."""
        result = db.run_sql(query)
        return json.dumps(result, ensure_ascii=False, default=str)

    @tool
    def now() -> str:
        """Şu anki tarih-saat (botun timezone'unda)."""
        return dt.datetime.now(TZ).isoformat(timespec="seconds")

    @tool
    def run_python(code: str) -> str:
        """Hesaplama, veri işleme ve grafik için Python kodu çalıştır.
        Her çağrı bağımsızdır (state taşınmaz).

        Hazır isimler: pd (pandas), plt (matplotlib.pyplot), db (modül), now_tz.
        Veri çekme: con = db.connect(); df = pd.read_sql('SELECT ...', con).

        Çıktı:
        - Sayı/text özet için print(...) kullan; stdout sana geri döner.
        - Tablo için send_table(headers, rows): headers ve her row string listesi;
          görsel otomatik gönderilir, metin tablo yazma.
        - Sadece ifade yazmak (örn. "df.mean()") çıktı vermez, print(df.mean()) yaz.
        - Grafik için plt.figure() + plt.plot/bar/hist...; savefig'e gerek yok,
          açık figürler otomatik kullanıcıya gönderilir.
        - print, send_table ve grafik aynı anda kullanılabilir."""
        stdout = io.StringIO()
        env: dict[str, Any] = {
            "pd": pd,
            "plt": plt,
            "db": db,
            "now_tz": dt.datetime.now(TZ),
            "send_table": _queue_table_image,
        }
        err: Optional[str] = None
        try:
            with contextlib.redirect_stdout(stdout):
                exec(code, env)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"

        chart_count = 0
        for num in plt.get_fignums():
            fig = plt.figure(num)
            path = Path(tempfile.gettempdir()) / f"chart_{uuid.uuid4().hex}.png"
            fig.savefig(path, dpi=110, bbox_inches="tight")
            plt.close(fig)
            buf = _pending_images.get(None)
            if buf is None:
                # Outside an invoke_agent scope: drop silently. The image
                # would be unreachable anyway.
                log.warning("run_python produced a chart outside invoke_agent scope; dropping")
            else:
                buf.append(str(path))
            chart_count += 1

        out = stdout.getvalue().strip()
        parts = []
        if err:
            parts.append(f"ERROR: {err}")
        if out:
            parts.append(f"stdout:\n{out}")
        if chart_count:
            parts.append(f"[{chart_count} grafik kullanıcıya gönderildi]")
        return "\n".join(parts) if parts else "OK (no output)"

    return [
        list_questions,
        get_question,
        add_question,
        update_question,
        delete_question,
        query_answers,
        delete_answer,
        list_scheduled_prompts,
        add_scheduled_prompt,
        update_scheduled_prompt,
        delete_scheduled_prompt,
        run_sql,
        run_python,
        now,
    ]


# ---------- agent ----------
def build_agent(
    reschedule_question: Callable[[str], None],
    reschedule_prompt: Callable[[str], None],
):
    log.info("Initializing LLM provider=%s model=%s", LLM_PROVIDER, LLM_MODEL)
    llm = init_chat_model(LLM_MODEL, model_provider=LLM_PROVIDER)
    tools = build_tools(reschedule_question, reschedule_prompt)
    return create_react_agent(llm, tools, state_modifier=SYSTEM_PROMPT)


async def invoke_agent(
    agent, history: list[dict[str, str]], user_text: str
) -> tuple[str, list[str]]:
    """Run the agent. Returns (reply_text, image_paths). Paths are chart or
    table PNGs; caller must read/send/unlink them."""
    buf: list[str] = []
    token = _pending_images.set(buf)
    try:
        messages = history + [{"role": "user", "content": user_text}]
        result = await agent.ainvoke({"messages": messages})
        last = result["messages"][-1]
        reply = getattr(last, "content", "") or ""
        return reply, list(buf)
    finally:
        _pending_images.reset(token)
