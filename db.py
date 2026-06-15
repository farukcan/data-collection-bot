"""PocketBase layer. Single owner, three collections: questions, answers, scheduled_prompts.

questions and scheduled_prompts use a `slug` text field for human-readable IDs because
PocketBase v0.23+ requires record IDs to be at least 15 characters.
All public functions accept/return the slug as `id`.
"""
import datetime as dt
import json
import logging
from typing import Any, Optional

import pandas as pd
import requests

from config import PB_EMAIL, PB_PASSWORD, PB_URL, SEED_FILE, TZ

log = logging.getLogger("quizbot.db")

VALID_TYPES = {"scale", "rating", "choice", "open"}

_SCHEMAS: dict[str, list[dict[str, Any]]] = {
    "questions": [
        {"name": "slug", "type": "text", "required": True},
        {"name": "type", "type": "text", "required": True},
        {"name": "text", "type": "text", "required": True},
        {"name": "config", "type": "json"},
        {"name": "cron", "type": "text", "required": True},
        {"name": "timeout_minutes", "type": "number", "required": True},
        {"name": "active", "type": "bool"},
    ],
    "answers": [
        {"name": "ts", "type": "text", "required": True},
        {"name": "day", "type": "text", "required": True},
        {"name": "qid", "type": "text", "required": True},
        {"name": "qtype", "type": "text", "required": True},
        {"name": "answer", "type": "text"},
    ],
    "scheduled_prompts": [
        {"name": "slug", "type": "text", "required": True},
        {"name": "prompt", "type": "text", "required": True},
        {"name": "cron", "type": "text", "required": True},
        {"name": "active", "type": "bool"},
    ],
}

_PB_META = {"collectionId", "collectionName", "created", "updated"}


def _fv(value: str) -> str:
    """Escape a string value for PocketBase filter expressions."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


class _PBClient:
    """Admin-auth PocketBase REST client."""

    def __init__(self, base_url: str, email: str, password: str) -> None:
        self._base = base_url.rstrip("/")
        self._email = email
        self._password = password
        self._token: str = ""

    def _auth(self) -> None:
        # PocketBase v0.23+ uses _superusers collection for admin auth
        resp = requests.post(
            f"{self._base}/api/collections/_superusers/auth-with-password",
            json={"identity": self._email, "password": self._password},
            timeout=10,
        )
        if not resp.ok:
            # Body reveals whether the block came from PocketBase (JSON) or a proxy like Cloudflare (HTML)
            log.error("PB auth failed (%s): %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
        self._token = resp.json()["token"]

    def _req(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        if not self._token:
            self._auth()
        resp = requests.request(
            method,
            f"{self._base}{path}",
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=10,
            **kwargs,
        )
        # An expired/invalid superuser token is treated as a guest by PocketBase,
        # so superuser-only collections answer 403 ("Only superusers can perform
        # this action."), not 401. Re-auth and retry once on both.
        if resp.status_code in (401, 403):
            self._auth()
            resp = requests.request(
                method,
                f"{self._base}{path}",
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=10,
                **kwargs,
            )
        resp.raise_for_status()
        return resp

    def list_all(
        self,
        col: str,
        filter_str: str = "",
        sort: str = "",
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        per_page = min(limit, 500) if limit else 500
        while True:
            params: dict[str, Any] = {"page": page, "perPage": per_page}
            if filter_str:
                params["filter"] = filter_str
            if sort:
                params["sort"] = sort
            data = self._req("GET", f"/api/collections/{col}/records", params=params).json()
            items.extend(data["items"])
            if limit and len(items) >= limit:
                return items[:limit]
            if len(items) >= data["totalItems"]:
                break
            page += 1
        return items

    def get_by_id(self, col: str, pb_id: str) -> Optional[dict[str, Any]]:
        try:
            return self._req("GET", f"/api/collections/{col}/records/{pb_id}").json()
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            raise

    def get_by_slug(self, col: str, slug: str) -> Optional[dict[str, Any]]:
        recs = self.list_all(col, filter_str=f'slug = "{_fv(slug)}"', limit=1)
        return recs[0] if recs else None

    def create(self, col: str, data: dict[str, Any]) -> dict[str, Any]:
        return self._req("POST", f"/api/collections/{col}/records", json=data).json()

    def update_by_id(self, col: str, pb_id: str, data: dict[str, Any]) -> dict[str, Any]:
        return self._req("PATCH", f"/api/collections/{col}/records/{pb_id}", json=data).json()

    def delete_by_id(self, col: str, pb_id: str) -> None:
        self._req("DELETE", f"/api/collections/{col}/records/{pb_id}")

    def ensure_collections(self) -> None:
        for name, fields in _SCHEMAS.items():
            try:
                self._req("GET", f"/api/collections/{name}")
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code != 404:
                    raise
                log.info("Creating PocketBase collection: %s", name)
                # PocketBase v0.23+ uses "fields" instead of "schema"
                self._req(
                    "POST",
                    "/api/collections",
                    json={"name": name, "type": "base", "fields": fields},
                )


_client: Optional[_PBClient] = None


def _pb() -> _PBClient:
    global _client
    if _client is None:
        _client = _PBClient(PB_URL, PB_EMAIL, PB_PASSWORD)
    return _client


def init() -> None:
    _pb().ensure_collections()


def seed_if_empty() -> int:
    if not SEED_FILE.exists():
        return 0
    if _pb().list_all("questions", limit=1):
        return 0
    data = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    meta = {"id", "type", "text", "cron", "timeout_minutes"}
    inserted = 0
    for q in data["questions"]:
        if q["type"] not in VALID_TYPES:
            continue
        config = {k: v for k, v in q.items() if k not in meta}
        insert_question(
            q["id"],
            q["type"],
            q["text"],
            config,
            q["cron"],
            int(q["timeout_minutes"]),
            active=1,
        )
        inserted += 1
    return inserted


# ---------- questions ----------

def _pb_to_question(rec: dict[str, Any]) -> dict[str, Any]:
    cfg = rec["config"]
    if isinstance(cfg, str):
        cfg = json.loads(cfg)
    return {
        "id": rec["slug"],
        "type": rec["type"],
        "text": rec["text"],
        "config": cfg if cfg else {},
        "cron": rec["cron"],
        "timeout_minutes": int(rec["timeout_minutes"]),
        "active": 1 if rec.get("active") else 0,
    }


def list_questions(active_only: bool) -> list[dict[str, Any]]:
    filter_str = "active=true" if active_only else ""
    recs = _pb().list_all("questions", filter_str=filter_str, sort="slug")
    return [_pb_to_question(r) for r in recs]


def get_question(qid: str) -> Optional[dict[str, Any]]:
    rec = _pb().get_by_slug("questions", qid)
    return _pb_to_question(rec) if rec else None


def _normalize_config(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError(f"config must be a JSON object, got {type(value).__name__}")
    return value


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
    _pb().create("questions", {
        "slug": qid,
        "type": qtype,
        "text": text,
        "config": _normalize_config(config),
        "cron": cron,
        "timeout_minutes": timeout_minutes,
        "active": bool(active),
    })


def update_question(qid: str, fields: dict[str, Any]) -> int:
    allowed = {"type", "text", "config", "cron", "timeout_minutes", "active"}
    data: dict[str, Any] = {}
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(f"Field not updatable: {k}")
        if k == "type" and v not in VALID_TYPES:
            raise ValueError(f"Invalid type: {v}")
        if k == "config":
            v = _normalize_config(v)
        if k == "active":
            v = bool(v)
        data[k] = v
    if not data:
        return 0
    rec = _pb().get_by_slug("questions", qid)
    if rec is None:
        return 0
    _pb().update_by_id("questions", rec["id"], data)
    return 1


def delete_question(qid: str) -> int:
    rec = _pb().get_by_slug("questions", qid)
    if rec is None:
        return 0
    try:
        _pb().delete_by_id("questions", rec["id"])
        return 1
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return 0
        raise


# ---------- answers ----------

def _pb_to_answer(rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": rec["id"],
        "ts": rec["ts"],
        "day": rec["day"],
        "qid": rec["qid"],
        "qtype": rec["qtype"],
        "answer": rec.get("answer"),
    }


def save_answer(qid: str, qtype: str, answer: str) -> None:
    now = dt.datetime.now(TZ)
    _pb().create("answers", {
        "ts": now.isoformat(timespec="seconds"),
        "day": now.date().isoformat(),
        "qid": qid,
        "qtype": qtype,
        "answer": answer,
    })


def query_answers(
    qid: Optional[str],
    since_day: Optional[str],
    until_day: Optional[str],
    since_ts: Optional[str],
    until_ts: Optional[str],
    limit: Optional[int],
) -> list[dict[str, Any]]:
    filters: list[str] = []
    if qid:
        filters.append(f'qid = "{_fv(qid)}"')
    if since_day:
        filters.append(f'day >= "{_fv(since_day)}"')
    if until_day:
        filters.append(f'day <= "{_fv(until_day)}"')
    if since_ts:
        filters.append(f'ts >= "{_fv(since_ts)}"')
    if until_ts:
        filters.append(f'ts <= "{_fv(until_ts)}"')
    filter_str = " && ".join(filters)
    recs = _pb().list_all("answers", filter_str=filter_str, sort="-ts", limit=limit)
    return [_pb_to_answer(r) for r in recs]


def delete_answer(answer_id: str) -> int:
    try:
        _pb().delete_by_id("answers", answer_id)
        return 1
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return 0
        raise


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


def _pb_to_prompt(rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": rec["slug"],
        "prompt": rec["prompt"],
        "cron": rec["cron"],
        "active": 1 if rec.get("active") else 0,
    }


def list_scheduled_prompts(active_only: bool) -> list[dict[str, Any]]:
    filter_str = "active=true" if active_only else ""
    recs = _pb().list_all("scheduled_prompts", filter_str=filter_str, sort="slug")
    return [_pb_to_prompt(r) for r in recs]


def get_scheduled_prompt(pid: str) -> Optional[dict[str, Any]]:
    rec = _pb().get_by_slug("scheduled_prompts", pid)
    return _pb_to_prompt(rec) if rec else None


def insert_scheduled_prompt(pid: str, prompt: str, cron: str, active: int) -> None:
    instruction = normalize_scheduled_prompt_instruction(prompt)
    _pb().create("scheduled_prompts", {
        "slug": pid,
        "prompt": instruction,
        "cron": cron,
        "active": bool(active),
    })


def update_scheduled_prompt(pid: str, fields: dict[str, Any]) -> int:
    allowed = {"prompt", "cron", "active"}
    data: dict[str, Any] = {}
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(f"Field not updatable: {k}")
        if k == "prompt":
            v = normalize_scheduled_prompt_instruction(v)
        if k == "active":
            v = bool(v)
        data[k] = v
    if not data:
        return 0
    rec = _pb().get_by_slug("scheduled_prompts", pid)
    if rec is None:
        return 0
    _pb().update_by_id("scheduled_prompts", rec["id"], data)
    return 1


def delete_scheduled_prompt(pid: str) -> int:
    rec = _pb().get_by_slug("scheduled_prompts", pid)
    if rec is None:
        return 0
    try:
        _pb().delete_by_id("scheduled_prompts", rec["id"])
        return 1
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return 0
        raise


# ---------- utilities ----------

def to_df(collection: str) -> pd.DataFrame:
    """Fetch all records from a collection and return as a pandas DataFrame.

    For `questions` and `scheduled_prompts`, the logical `id` (slug) is returned
    as `id`, not the raw PocketBase 15-char record ID.
    """
    allowed = {"questions", "answers", "scheduled_prompts"}
    if collection not in allowed:
        raise ValueError(f"Unknown collection: {collection}. Must be one of {allowed}")
    recs = _pb().list_all(collection)
    if not recs:
        return pd.DataFrame()
    rows = []
    for r in recs:
        row = {k: v for k, v in r.items() if k not in _PB_META}
        if collection in ("questions", "scheduled_prompts"):
            row["id"] = row.pop("slug")
        rows.append(row)
    return pd.DataFrame(rows)


def dump_all() -> dict[str, Any]:
    """Return all records from every collection as plain dicts."""
    return {
        "questions": list_questions(active_only=False),
        "answers": query_answers(None, None, None, None, None, None),
        "scheduled_prompts": list_scheduled_prompts(active_only=False),
    }
