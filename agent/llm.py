"""One Claude call that must return JSON matching a schema (structured outputs). Claude gets no tools: it only decides
from the text it is given."""
import json
import os
import re

import anthropic

from .db import recorded

_client = None
# $ per million tokens [input, output, cache read (default: a tenth of input)].
PRICES = {"claude-sonnet-5-5": (2, 10, 0.1), "claude-haiku-5-5": (0.1, 0.5), "claude-opus-5-5": (4, 20)}


def model():
    return os.environ.get("CLAUDE_MODEL") or "claude-sonnet-5-5"


def _usage(u, name=None):
    price = PRICES.get(name or model())
    written, read = u.get("cache_creation_input_tokens") or 0, u.get("cache_read_input_tokens") or 0
    input_tokens = u["input_tokens"] + written + read
    if not price:
        return {"input_tokens": input_tokens, "output_tokens": u["output_tokens"], "usd": None}
    # Cached instructions: writing them costs 1.25x the input price; reading them back has its own price.
    read_price = price[2] if len(price) > 2 else price[0] * 0.1
    usd = ((u["input_tokens"] + written * 1.25) * price[0] + read * read_price + u["output_tokens"] * price[1]) / 1e6
    return {"input_tokens": input_tokens, "output_tokens": u["output_tokens"], "usd": usd}


async def ask_json(system, prompt, schema, effort="medium", max_tokens=16000):
    """Returns {"data", "usage"}; usage["usd"] is None for a model without a listed price."""
    global _client
    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
    if not key:
        raise RuntimeError("Set ANTHROPIC_API_KEY (or CLAUDE_API_KEY) in .env to run the agent.")
    _client = _client or anthropic.AsyncAnthropic(api_key=key, max_retries=4)

    def call(strict):
        async def go():
            fmt = {"format": {"type": "json_schema", "schema": schema}} if strict else {}
            text_system = system if strict else f"{system}\n\nReply with only one JSON object that matches this JSON schema, no other text:\n{json.dumps(schema)}"
            # The instructions are the same for every company: cached, later calls in the next few minutes read them at a tenth of the price.
            cached = [{"type": "text", "text": text_system, "cache_control": {"type": "ephemeral"}}]
            async with _client.messages.stream(model=model(), max_tokens=max_tokens, system=cached, messages=[{"role": "user", "content": prompt}],
                                               output_config={"effort": effort, **fmt}) as stream:
                return (await stream.get_final_message()).model_dump(mode="json")
        return go

    def meta(raw):
        u = _usage(raw["usage"])
        return {"input_tokens": u["input_tokens"], "output_tokens": u["output_tokens"], "cost": u["usd"]}

    request = {"model": model(), "system": system, "prompt": prompt}
    key_ = json.dumps(schema, sort_keys=True)
    msg = None
    if key_ not in _too_large:
        try:
            msg = await recorded("anthropic", "decide", request, call(True), meta)
        except anthropic.BadRequestError as error:
            # The API can refuse a schema as too complex to enforce ("compiled grammar is too large"). Rather than fail the
            # company, ask with the schema written into the instructions and fill out the reply to the schema below.
            # Remembered, so later calls with this schema skip the refused request.
            if not re.search(r"grammar|schema", str(error), re.I):
                raise
            _too_large.add(key_)
    usage = None
    for attempt in (1, 2):
        if msg is None:
            msg = await recorded("anthropic", "decide", {**request, "strict": False}, call(False), meta)
            usage = _add(usage, msg["usage"])
        if msg.get("stop_reason") == "refusal":
            raise RuntimeError("The model declined this request.")
        if msg.get("stop_reason") == "max_tokens":
            raise RuntimeError("The model ran out of output tokens.")
        text = "".join(b.get("text", "") for b in msg["content"] if b.get("type") == "text")
        try:
            data = _json_in(text)
            break
        except (ValueError, RuntimeError):
            if attempt == 2 or key_ not in _too_large:
                raise RuntimeError("The model's reply wasn't valid JSON.") from None
            msg = None  # without strict mode a reply can rarely be malformed: ask once more
    return {"data": conform(data, schema), "usage": _usage(usage or msg["usage"])}


_too_large = set()  # schemas the API refused to enforce in this process


def _add(total, u):
    """Token usage summed over a retry, so the cost shown includes both calls."""
    if total is None:
        return dict(u)
    return {k: (total.get(k) or 0) + (u.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}


def _json_in(text):
    """The JSON object in a reply, even if wrapped in a ``` fence or a sentence."""
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end < start:
            raise RuntimeError("The model's reply wasn't JSON.") from None
        return json.loads(text[start:end + 1])


def conform(value, schema):
    """Shape a value to the schema: missing fields get empty defaults and wrong types are replaced, so the code reading it never breaks."""
    kind = schema.get("type")
    if kind == "object":
        value = value if isinstance(value, dict) else {}
        return {k: conform(value.get(k), sub) for k, sub in schema.get("properties", {}).items()}
    if kind == "array":
        return [conform(v, schema.get("items", {})) for v in value] if isinstance(value, list) else []
    if kind == "string":
        value = "" if value is None or isinstance(value, (dict, list)) else str(value)
        return value if not schema.get("enum") or value in schema["enum"] else schema["enum"][-1]
    if kind == "integer":
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
    if kind == "boolean":
        return value if isinstance(value, bool) else False
    return value


def chat_model():
    """The chat assistant answers from research it is handed, so the small, cheap model does it well (about 20x cheaper)."""
    return os.environ.get("CHAT_MODEL") or "claude-haiku-5-5"


async def ask_text(system, context, messages, max_tokens=900):
    """A plain-text answer for the chat. `context` (the company's research) is cached, so follow-ups re-read it at a tenth of the price."""
    global _client
    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
    if not key:
        raise RuntimeError("Set ANTHROPIC_API_KEY (or CLAUDE_API_KEY) in .env to use the chat.")
    _client = _client or anthropic.AsyncAnthropic(api_key=key, max_retries=4)
    name = chat_model()
    blocks = [{"type": "text", "text": system}, {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}}]

    async def go():
        return (await _client.messages.create(model=name, max_tokens=max_tokens, system=blocks, messages=messages)).model_dump(mode="json")

    def meta(raw):
        u = _usage(raw["usage"], name)
        return {"input_tokens": u["input_tokens"], "output_tokens": u["output_tokens"], "cost": u["usd"]}
    msg = await recorded("anthropic", "chat", {"model": name, "messages": len(messages)}, go, meta)
    text = "".join(b.get("text", "") for b in msg["content"] if b.get("type") == "text").strip()
    return {"text": text or "I couldn't produce an answer to that. Try rephrasing it.", "usage": _usage(msg["usage"], name)}
