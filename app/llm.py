"""The only Anthropic call. Tokens are added to the current runs row."""
import json
import re
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from uuid import uuid4

from .db import connect, current_run_id, now
from .settings import load

DEFAULT_DAILY = 400_000
DEFAULT_RUN = 150_000
RETRYABLE = {429, 500, 502, 503, 504, 529}
source_text = ContextVar("llm_source_text", default="")


class BudgetExhausted(Exception):
    """The daily or per-run token budget is already spent."""


class LLMRejected(Exception):
    """The model output was missing or did not match the schema."""


def _sleep(seconds):
    time.sleep(seconds)


def _client(api_key):
    import anthropic
    return anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=60)


def _number(settings, name, default):
    raw = settings.get(name, "")
    try:
        value = int(raw) if str(raw).strip() else default
    except (TypeError, ValueError):
        return default
    return max(value, 0)


def _spent(settings, run_id):
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    with connect(settings.db_path) as db:
        daily = db.execute("SELECT COALESCE(SUM(tokens_in + tokens_out), 0) FROM runs WHERE started_at>=?", (start,)).fetchone()[0]
        current = 0
        if run_id:
            row = db.execute("SELECT tokens_in, tokens_out FROM runs WHERE id=?", (run_id,)).fetchone()
            if row:
                current = (row["tokens_in"] or 0) + (row["tokens_out"] or 0)
    return int(daily), int(current)


def _field(obj, name):
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _tokens(usage):
    incoming = int(_field(usage, "input_tokens") or 0)
    incoming += int(_field(usage, "cache_read_input_tokens") or 0)
    incoming += int(_field(usage, "cache_creation_input_tokens") or 0)
    return incoming, int(_field(usage, "output_tokens") or 0)


def _charge(settings, purpose, incoming, outgoing):
    run_id = current_run_id.get()
    kind = "".join(char for char in purpose.lower() if char.isalnum() or char == "_")[:40] or "llm"
    with connect(settings.db_path) as db:
        if run_id and db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
            db.execute("UPDATE runs SET tokens_in=COALESCE(tokens_in,0)+?, tokens_out=COALESCE(tokens_out,0)+? WHERE id=?", (incoming, outgoing, run_id))
            return
        db.execute("INSERT INTO runs (id,business_id,kind,started_at,finished_at,status,tokens_in,tokens_out) VALUES (?,?,?,?,?,?,?,?)",
                   (str(uuid4()), None, kind, now(), now(), "ok", incoming, outgoing))


def _json_text(text):
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _message_parts(message):
    text, corpus = [], []
    for block in _field(message, "content") or []:
        kind = _field(block, "type")
        if kind == "text":
            text.append(_field(block, "text") or "")
        elif kind == "web_search_tool_result":
            content = _field(block, "content")
            if isinstance(content, list):
                for item in content:
                    corpus.append(str(_field(item, "title") or ""))
                    corpus.append(str(_field(item, "url") or ""))
    return "".join(text), "\n".join(part for part in corpus if part)


def _matches(schema, value):
    expected = schema.get("type")
    if isinstance(expected, list):
        return any(_matches({**schema, "type": item}, value) for item in expected)
    if expected == "object":
        if not isinstance(value, dict):
            return False
        props = schema.get("properties") or {}
        if any(key not in value for key in schema.get("required") or []):
            return False
        if schema.get("additionalProperties") is False and any(key not in props for key in value):
            return False
        return all(key not in props or _matches(props[key], item) for key, item in value.items())
    if expected == "array":
        if not isinstance(value, list):
            return False
        if len(value) < schema.get("minItems", 0) or ("maxItems" in schema and len(value) > schema["maxItems"]):
            return False
        item_schema = schema.get("items")
        return item_schema is None or all(_matches(item_schema, item) for item in value)
    if expected == "string":
        return isinstance(value, str) and (schema.get("enum") is None or value in schema["enum"])
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def ask(system: str, user: str, *, schema: dict | None = None, web_search: bool = False, max_uses: int = 5, purpose: str) -> dict:
    import anthropic
    settings = load()
    api_key = settings.get("ANTHROPIC_API_KEY")
    model = settings.get("ANTHROPIC_MODEL")
    if not api_key or not model:
        raise LLMRejected("ANTHROPIC_API_KEY and ANTHROPIC_MODEL are required")
    daily_spent, run_spent = _spent(settings, current_run_id.get())
    if daily_spent >= _number(settings, "DAILY_TOKEN_BUDGET", DEFAULT_DAILY) or run_spent >= _number(settings, "PER_RUN_TOKEN_BUDGET", DEFAULT_RUN):
        raise BudgetExhausted("token budget is spent")
    kwargs = {"model": model, "max_tokens": 4096, "system": system, "messages": [{"role": "user", "content": user}]}
    if schema is not None:
        kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    if web_search:
        kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses,
                            "user_location": {"type": "approximate", "country": "US"}}]
    delay = 0.5
    for attempt in range(3):
        try:
            message = _client(api_key).messages.create(**kwargs)
            break
        except anthropic.APIStatusError as exc:
            if exc.status_code not in RETRYABLE or attempt == 2:
                raise
        except anthropic.APIConnectionError:
            if attempt == 2:
                raise
        _sleep(delay)
        delay *= 2
    incoming, outgoing = _tokens(_field(message, "usage"))
    _charge(settings, purpose, incoming, outgoing)
    text, corpus = _message_parts(message)
    source_text.set(corpus)
    try:
        payload = json.loads(_json_text(text))
    except json.JSONDecodeError:
        raise LLMRejected("model output was not JSON") from None
    if not isinstance(payload, dict) or (schema is not None and not _matches(schema, payload)):
        raise LLMRejected("model output did not match the schema")
    return payload
