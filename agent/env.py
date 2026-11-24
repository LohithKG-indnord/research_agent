"""Dependency-free .env loader; real environment variables win."""
import os
import re
from pathlib import Path

try:
    for line in (Path(__file__).resolve().parent.parent / ".env").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if m and m.group(2) and not os.environ.get(m.group(1)):
            os.environ[m.group(1)] = re.sub(r"^['\"]|['\"]$", "", m.group(2))
except OSError:
    pass  # .env is optional
