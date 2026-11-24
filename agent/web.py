"""Web access: search providers and page fetching."""
import asyncio
import ipaddress
import json
import math
import os
import re
import subprocess
import sys
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .db import recorded

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def warn(message):
    print(message, file=sys.stderr)


async def http(method, url, *, headers=None, json_body=None, content=None, timeout=20):
    """Every outside HTTP call goes through here (tests replace it). Redirects are never followed automatically."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        return await client.request(method, url, headers=headers, json=json_body, content=content)


ENTITIES = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'", "nbsp": " ", "#39": "'"}


def decode(s):
    def one(m):
        e = m.group(1)
        if e[0] == "#":
            try:
                return chr(int(e[2:], 16) if e[1:2].lower() == "x" else int(e[1:]))
            except (ValueError, OverflowError):
                return m.group(0)
        return ENTITIES.get(e.lower(), m.group(0))
    return re.sub(r"&(#x?[0-9a-f]+|\w+);", one, str(s or ""), flags=re.I)


def strip(s):
    return re.sub(r"\s+", " ", decode(re.sub(r"<[^>]+>", " ", str(s or "")))).strip()


def _host(url):
    """Lower-case host of an http(s) URL, or None. IPv6 literals keep their brackets, as browsers write them."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    if not host or re.search(r"\s", host):
        return None
    return f"[{host}]" if ":" in host else host.lower()


def domain_of(value):
    value = str(value or "")
    host = _host(value if re.match(r"^https?://", value, re.I) else f"https://{value}")
    return re.sub(r"^www\.", "", host) if host else ""


# Never fetch private/internal hosts or social networks (design doc section 10).
BLOCKED_HOST = re.compile(r"^(localhost|.*\.local|.*\.internal|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.|0\.|\[)|(^|\.)linkedin\.com$|(^|\.)facebook\.com$|(^|\.)instagram\.com$|(^|\.)x\.com$|(^|\.)twitter\.com$", re.I)


def fetchable(url):
    try:
        scheme = urlsplit(str(url)).scheme.lower()
    except ValueError:
        return False
    host = _host(str(url))
    return scheme in ("http", "https") and bool(host) and not BLOCKED_HOST.search(host)


PRIVATE_NETS = [ipaddress.ip_network(n) for n in ("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16", "::/127", "fc00::/7", "fe80::/10")]


def is_private(ip):
    addr = ipaddress.ip_address(ip.split("%")[0])
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in net for net in PRIVATE_NETS if net.version == addr.version)


async def safe_fetch(url, headers=None, timeout=20):
    """Follow redirects by hand so every hop passes the host rules, not only the first URL."""
    for _ in range(6):
        # shortcut: the request resolves DNS again after this check; pin the IP if the agent runs inside a sensitive network.
        if not fetchable(url):
            return None
        infos = await asyncio.get_running_loop().getaddrinfo(urlsplit(url).hostname, None)
        if any(is_private(info[4][0]) for info in infos):
            return None
        res = await http("GET", url, headers=headers, timeout=timeout)
        location = res.headers.get("location") if 300 <= res.status_code < 400 else None
        if not location:
            return res, url
        url = urljoin(url, location)
    return None


def same_site(host, domain):
    """True when `host` is `domain` or one of its subdomains."""
    return bool(domain) and (host == domain or host.endswith(f".{domain}"))


async def exa_search(query, max_results, category=None):
    async def go():
        res = await http("POST", "https://api.exa.ai/search", headers={"Content-Type": "application/json", "x-api-key": os.environ.get("EXA_API_KEY", "")},
                         json_body={"query": query, "type": "auto", "numResults": max_results, "contents": {"highlights": {"maxCharacters": 1200}},
                                    **({"category": category} if category else {})}, timeout=20)
        if not res.is_success:
            raise RuntimeError(f"Exa returned {res.status_code}")
        return res.json()
    data = await recorded("exa", "search", {"query": query, "max": max_results, "category": category}, go, lambda raw: {"cost": (raw.get("costDollars") or {}).get("total")})
    return {
        "rows": [{"title": r.get("title") or "", "url": r.get("url"), "snippet": " ".join(r.get("highlights") or []), "date": r.get("publishedDate") or ""} for r in data.get("results") or []],
        "cost": (data.get("costDollars") or {}).get("total"),
    }


def donsetch_bin():
    """The npm shim is a .cmd that can't be run directly, so call the binary itself (no shell: queries hold user text)."""
    return os.environ.get("DONSETCH_BINARY") or os.path.join(os.environ.get("APPDATA", ""), "npm/node_modules/donsetch/binaries/donsetch.exe")


async def donsetch_search(query, max_results):
    def run():
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        out = subprocess.run([donsetch_bin(), "search", "--json", "--quiet", "--max-results", str(max_results), "--", query], capture_output=True, timeout=45, check=True, creationflags=flags)
        return json.loads(out.stdout.decode("utf-8", "replace"))
    data = await recorded("donsetch", "search", {"query": query, "max": max_results}, lambda: asyncio.to_thread(run), lambda raw: {"cost": 0})
    return {"rows": [{"title": r.get("title") or "", "url": r.get("url"), "snippet": r.get("snippet") or "", "date": ""} for r in data["meta"]["results"]], "cost": 0}


engines = {"exa": exa_search, "donsetch": donsetch_search}  # a dict so tests can replace them


def _plain(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


async def search(query, max_results=8, about="", also_exa=False):
    """DonSeTch (free) first. Exa (paid) runs only when DonSeTch fails or finds fewer than half `max_results` results that
    mention `about` (the company); the two are then merged, results about the company first.
    `also_exa`: always add Exa, listed first (its longer page extracts often hold figures, e.g. revenue, that DonSeTch's snippets miss)."""
    name = re.sub(r" (inc|llc|ltd|corp|corporation|company|pvt|private|limited)$", "", _plain(about))

    def relevant(r):
        return not name or name in _plain(f"{r['title']} {r['snippet']} {r['url']}")

    async def try_engine(provider):
        try:
            got = await engines[provider](query, max_results)
            return {"rows": [r for r in got["rows"] if fetchable(r["url"])], "cost": got["cost"]}
        except Exception as error:  # noqa: BLE001 - one engine failing must not stop the search
            warn(f"{provider} search failed: {str(error)[:200]}")
            return {"rows": [], "cost": 0}

    use_exa = also_exa and bool(os.environ.get("EXA_API_KEY"))
    free, early = await asyncio.gather(try_engine("donsetch"), try_engine("exa") if use_exa else asyncio.sleep(0, None))
    if not use_exa and (len([r for r in free["rows"] if relevant(r)]) >= math.ceil(max_results / 2) or not os.environ.get("EXA_API_KEY")):
        return {"rows": free["rows"][:max_results], "provider": "donsetch" if free["rows"] else "none", "cost": 0}
    paid = early or await try_engine("exa")
    merged = {}
    for r in (paid["rows"] + free["rows"]) if use_exa else (free["rows"] + paid["rows"]):
        merged[r["url"]] = r  # like a JavaScript Map: a repeat keeps its first place but takes the later row
    unique = list(merged.values())
    rows = ([r for r in unique if relevant(r)] + [r for r in unique if not relevant(r)])[:max_results]
    used = "+".join(p for p, got in (("donsetch", free), ("exa", paid)) if got["rows"])
    return {"rows": rows, "provider": used or "none", "cost": paid["cost"]}


async def fetch_page(url, max_chars=6000):
    """Fetch a page and reduce it to readable text plus title, description and same-site links."""
    if not fetchable(url):
        return None
    for attempt in (1, 2, 3):
        page = await fetch_once(url, max_chars)
        if page != "retry":
            return page
        if attempt < 3:
            await asyncio.sleep(1.5 * attempt)
    warn(f"fetch failed after retries: {url}")
    return None


def _href(url):
    """A URL written the way browsers normalise it (lower-case host, "/" for an empty path), so duplicates match."""
    p = urlsplit(url)
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", p.query, p.fragment))


async def fetch_once(url, max_chars):
    """Returns a page, None (permanent: 4xx / not HTML) or "retry" (timeout, network error, 429, 5xx)."""
    try:
        hit = await safe_fetch(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9"}, timeout=20)
        if not hit:
            return None
        res, final_url = hit
        if res.status_code == 429 or res.status_code >= 500:
            return "retry"
        if not res.is_success or not re.search(r"text/html|xhtml", res.headers.get("content-type", ""), re.I):
            return None
        html = res.content.decode("utf-8", "replace")[:1_500_000]

        def meta(name):
            m = re.search(rf"<meta[^>]+(?:name|property)=[\"']{name}[\"'][^>]*content=[\"']([^\"']*)", html, re.I)
            return strip(m.group(1)) if m else ""

        body = re.sub(r"<!--[\s\S]*?-->", " ", re.sub(r"<(script|style|noscript|svg|template|iframe)[\s\S]*?</\1>", " ", html, flags=re.I))
        links = []
        for m in re.finditer(r"<a[^>]+href=[\"']([^\"'#]+)[\"']", body, re.I):
            try:
                link = _href(urljoin(final_url, decode(m.group(1))))
            except ValueError:
                continue
            if link and domain_of(link) == domain_of(final_url) and link not in links:
                links.append(link)
        title = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, re.I)
        return {"url": final_url, "title": strip(title.group(1)) if title else "", "description": meta("description") or meta("og:description"), "text": strip(body)[:max_chars], "links": links}
    except Exception:  # noqa: BLE001 - any network problem is worth another try
        return "retry"


async def map_limit(items, limit, fn):
    """Run `fn` over `items` with at most `limit` in flight. Keeps input order."""
    gate = asyncio.Semaphore(limit)

    async def one(item):
        async with gate:
            return await fn(item)
    return list(await asyncio.gather(*(one(item) for item in items)))
