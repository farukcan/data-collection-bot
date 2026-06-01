"""SQLite layer. Single owner, three tables: questions, answers, scheduled_prompts."""
import datetime as dt
import json
import sqlite3
from typing import Any, Optional

from config import DB, SEED_FILE, TZ

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    text            TEXT NOT NULL,
    config          TEXT NOT NULL,
    cron            TEXT NOT NULL,
    timeout_minutes INTEGER NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS answers (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    day    TEXT NOT NULL,
    qid    TEXT NOT NULL,
    qtype  TEXT NOT NULL,
    answer TEXT
);
CREATE TABLE IF NOT EXISTS scheduled_prompts (
    id     TEXT PRIMARY KEY,
    prompt TEXT NOT NULL,
    cron   TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
"""

VALID_TYPES = {"scale", "rating", "choice", "open"}


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connect() as con:
        con.executescript(SCHEMA)


def seed_if_empty() -> int:
    if not SEED_FILE.exists():
        return 0
    with connect() as con:
        count = con.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    if count > 0:
        return 0
    data = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    meta = {"id", "type", "text", "cron", "timeout_minutes"}
    inserted = 0
    with connect() as con:
        for q in data["questions"]:
            if q["type"] not in VALID_TYPES:
                continue
            config = {k: v for k, v in q.items() if k not in meta}
            con.execute(
                "INSERT INTO questions (id, type, text, config, cron, timeout_minutes, active) "
                "VALUES (?,?,?,?,?,?,1)",
                (
                    q["id"],
                    q["type"],
                    q["text"],
                    json.dumps(config, ensure_ascii=False),
                    q["cron"],
                    int(q["timeout_minutes"]),
                ),
            )
            inserted += 1
    return inserted


# ---------- questions ----------
def _row_to_question(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "type": row["type"],
        "text": row["text"],
        "config": json.loads(row["config"]),
        "cron": row["cron"],
        "timeout_minutes": row["timeout_minutes"],
        "active": row["active"],
    }


def list_questions(active_only: bool) -> list[dict[str, Any]]:
    sql = "SELECT * FROM questions"
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY id"
    with connect() as con:
        return [_row_to_question(r) for r in con.execute(sql).fetchall()]


def get_question(qid: str) -> Optional[dict[str, Any]]:
    with connect() as con:
        row = con.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    return _row_to_question(row) if row else None


def _normalize_config(value: Any) -> str:
    """Accept dict or JSON string; always store as a single-encoded JSON string."""
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError(f"config must be a JSON object, got {type(value).__name__}")
    return json.dumps(value, ensure_ascii=False)


def insert_question(
    qid: str,
    qtype: str,
    text: str,
    config: Any,
    cron: str,
    timeout_minutes: int,
    active: int,
) -> None:
    if qtype not in VALID_TYPES:
        raise ValueError(f"Invalid type: {qtype}. Must be one of {VALID_TYPES}")
    with connect() as con:
        con.execute(
            "INSERT INTO questions (id, type, text, config, cron, timeout_minutes, active) "
            "VALUES (?,?,?,?,?,?,?)",
            (qid, qtype, text, _normalize_config(config), cron, timeout_minutes, active),
        )


def update_question(qid: str, fields: dict[str, Any]) -> int:
    allowed = {"type", "text", "config", "cron", "timeout_minutes", "active"}
    sets = []
    params: list[Any] = []
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(f"Field not updatable: {k}")
        if k == "type" and v not in VALID_TYPES:
            raise ValueError(f"Invalid type: {v}")
        if k == "config":
            v = _normalize_config(v)
        sets.append(f"{k}=?")
        params.append(v)
    if not sets:
        return 0
    params.append(qid)
    with connect() as con:
        cur = con.execute(f"UPDATE questions SET {', '.join(sets)} WHERE id=?", params)
        return cur.rowcount


def delete_question(qid: str) -> int:
    with connect() as con:
        cur = con.execute("DELETE FROM questions WHERE id=?", (qid,))
        return cur.rowcount


# ---------- answers ----------
def save_answer(qid: str, qtype: str, answer: str) -> None:
    now = dt.datetime.now(TZ)
    with connect() as con:
        con.execute(
            "INSERT INTO answers (ts, day, qid, qtype, answer) VALUES (?,?,?,?,?)",
            (now.isoformat(timespec="seconds"), now.date().isoformat(), qid, qtype, answer),
        )


def query_answers(
    qid: Optional[str],
    since_day: Optional[str],
    until_day: Optional[str],
    since_ts: Optional[str],
    until_ts: Optional[str],
    limit: Optional[int],
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM answers WHERE 1=1"
    params: list[Any] = []
    if qid:
        sql += " AND qid=?"
        params.append(qid)
    if since_day:
        sql += " AND day>=?"
        params.append(since_day)
    if until_day:
        sql += " AND day<=?"
        params.append(until_day)
    if since_ts:
        sql += " AND ts>=?"
        params.append(since_ts)
    if until_ts:
        sql += " AND ts<=?"
        params.append(until_ts)
    sql += " ORDER BY ts DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with connect() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def delete_answer(answer_id: int) -> int:
    with connect() as con:
        cur = con.execute("DELETE FROM answers WHERE id=?", (answer_id,))
        return cur.rowcount


# ---------- scheduled_prompts ----------
def normalize_scheduled_prompt_instruction(prompt: str) -> str:
    """Return trimmed instruction text for scheduled_prompts.prompt.

    The prompt column stores imperative LLM instructions (what to do on each
    cron tick), not conversational questions or chat framing.
    """
    text = prompt.strip()
    if not text:
        raise ValueError("scheduled prompt instruction cannot be empty")
    return text


def list_scheduled_prompts(active_only: bool) -> list[dict[str, Any]]:
    sql = "SELECT * FROM scheduled_prompts"
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY id"
    with connect() as con:
        return [dict(r) for r in con.execute(sql).fetchall()]


def get_scheduled_prompt(pid: str) -> Optional[dict[str, Any]]:
    with connect() as con:
        row = con.execute("SELECT * FROM scheduled_prompts WHERE id=?", (pid,)).fetchone()
    return dict(row) if row else None


def insert_scheduled_prompt(pid: str, prompt: str, cron: str, active: int) -> None:
    instruction = normalize_scheduled_prompt_instruction(prompt)
    with connect() as con:
        con.execute(
            "INSERT INTO scheduled_prompts (id, prompt, cron, active) VALUES (?,?,?,?)",
            (pid, instruction, cron, active),
        )


def update_scheduled_prompt(pid: str, fields: dict[str, Any]) -> int:
    allowed = {"prompt", "cron", "active"}
    sets = []
    params: list[Any] = []
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(f"Field not updatable: {k}")
        if k == "prompt":
            v = normalize_scheduled_prompt_instruction(v)
        sets.append(f"{k}=?")
        params.append(v)
    if not sets:
        return 0
    params.append(pid)
    with connect() as con:
        cur = con.execute(f"UPDATE scheduled_prompts SET {', '.join(sets)} WHERE id=?", params)
        return cur.rowcount


def delete_scheduled_prompt(pid: str) -> int:
    with connect() as con:
        cur = con.execute("DELETE FROM scheduled_prompts WHERE id=?", (pid,))
        return cur.rowcount


# ---------- raw sql ----------
def run_sql(query: str) -> dict[str, Any]:
    """Single-statement SQL. Returns {'rows': [...]} for SELECT, {'rowcount': N} otherwise."""
    with connect() as con:
        cur = con.execute(query)
        if cur.description is not None:
            rows = [dict(r) for r in cur.fetchall()]
            return {"rows": rows}
        return {"rowcount": cur.rowcount}
