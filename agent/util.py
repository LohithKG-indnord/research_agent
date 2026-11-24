"""Small helpers shared by the agent modules, matching the JavaScript behaviour they replace."""
import json
import math
from datetime import datetime, timezone


def now_iso():
    """UTC time like JavaScript's toISOString(): 2026-11-23T03:02:56.205Z (stored text is compared as strings)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def jround(x):
    """Math.round: halves round up (Python's round() rounds halves to even)."""
    return math.floor(x + 0.5)


def to_json(value, indent=None):
    """JSON.stringify: same text, so hashes and files match what the JavaScript version wrote."""
    if indent is None:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(value, ensure_ascii=False, indent=indent)
