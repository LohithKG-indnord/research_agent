"""Web access: search (Exa's official API) and polite page reading (honest name, robots.txt obeyed, paced per site)."""
import asyncio
import ipaddress
import os
import re
import sys
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from .db import recorded

BOT = "AccountFitAgent"
# An honest name, never a browser disguise, so site owners can see who reads them and can block us in robots.txt.
UA = f"{BOT}/1.0 (+{os.environ.get('CRAWLER_CONTACT') or 'company research; obeys robots.txt'})"
MIN_GAP = 1.0        # seconds between two requests to the same site
MAX_DELAY = 10       # a robots.txt Crawl-delay above this: the site is skipped rather than read slowly
ROBOTS_TTL = 86_400  # RFC 9309: a cached robots.txt is used for at most 24 hours
_robots = {}         # origin -> (time read, RobotFileParser, or None meaning "read nothing here")
_next = {}           # host -> earliest time of the next request to it


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


async def _load_robots(origin):
    """RFC 9309: a 4xx (including no robots.txt) means no rules; a 5xx, 429 or unreachable server means read nothing."""
    try:
        hit = await safe_fetch(f"{origin}/robots.txt", headers={"User-Agent": UA}, timeout=10)
    except Exception:  # noqa: BLE001 - unreachable
        return None
    rules = RobotFileParser()
    if hit and (hit[0].status_code == 429 or hit[0].status_code >= 500):
        return None
    rules.parse(hit[0].content.decode("utf-8", "replace")[:500_000].splitlines() if hit and hit[0].is_success else [])
    return rules


async def robots_for(url):
    p = urlsplit(url)
    origin = f"{p.scheme}://{p.netloc}".lower()
    cached = _robots.get(origin)
    if not cached or time.monotonic() - cached[0] > ROBOTS_TTL:
        cached = _robots[origin] = (time.monotonic(), await _load_robots(origin))
    return cached[1]


async def pace(host, delay):
    """Wait until this site's next free slot, so requests to one site are at least `delay` seconds apart."""
    now = time.monotonic()
    at = max(now, _next.get(host, 0))
    _next[host] = at + max(MIN_GAP, delay)
    if at > now:
        await asyncio.sleep(at - now)


async def safe_fetch(url, headers=None, timeout=20, polite=False):
    """Follow redirects by hand so every hop passes the host rules, not only the first URL.
    polite: every hop must also be allowed by its site's robots.txt, and is paced per site (page reads use this)."""
    for _ in range(6):
        # shortcut: the request resolves DNS again after this check; pin the IP if the agent runs inside a sensitive network.
        if not fetchable(url):
            return None
        infos = await asyncio.get_running_loop().getaddrinfo(urlsplit(url).hostname, None)
        if any(is_private(info[4][0]) for info in infos):
            return None
        if polite:
            rules = await robots_for(url)
            delay = float((rules.crawl_delay(BOT) or 0) if rules else 0)
            if not rules or not rules.can_fetch(BOT, url) or delay > MAX_DELAY:
                return None
            await pace(_host(url), delay)
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


def _plain(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


async def search(query, max_results=8, about=""):
    """Web search through Exa's official, keyed API (its own index, licensed for automated use; no search engine is
    scraped). Results that mention `about` (the company) come first. No key or an Exa error -> no rows; callers handle that."""
    if not os.environ.get("EXA_API_KEY"):
        warn("Web search is off: set EXA_API_KEY in .env")
        return {"rows": [], "provider": "none", "cost": 0}
    try:
        got = await exa_search(query, max_results)
    except Exception as error:  # noqa: BLE001 - one failed search must not stop the run
        warn(f"exa search failed: {str(error)[:200]}")
        return {"rows": [], "provider": "none", "cost": 0}
    name = re.sub(r" (inc|llc|ltd|corp|corporation|company|pvt|private|limited)$", "", _plain(about))
    rows = [r for r in got["rows"] if fetchable(r["url"])]
    rows = sorted(rows, key=lambda r: bool(name) and name not in _plain(f"{r['title']} {r['snippet']} {r['url']}"))[:max_results]
    return {"rows": rows, "provider": "exa" if rows else "none", "cost": got["cost"]}


PAGE_DEADLINE = 25  # seconds for one page, retries included: one stalled site must not hold up a whole run


async def fetch_page(url, max_chars=6000):
    """Fetch a page and reduce it to readable text plus title, description and same-site links."""
    if not fetchable(url):
        return None

    async def attempts():
        for attempt in (1, 2, 3):
            page = await fetch_once(url, max_chars)
            if page != "retry":
                return page
            if attempt < 3:
                await asyncio.sleep(1.5 * attempt)
        warn(f"fetch failed after retries: {url}")
        return None
    try:
        return await asyncio.wait_for(attempts(), PAGE_DEADLINE)
    except TimeoutError:
        warn(f"fetch gave up after {PAGE_DEADLINE}s: {url}")
        return None


def _href(url):
    """A URL written the way browsers normalise it (lower-case host, "/" for an empty path), so duplicates match."""
    p = urlsplit(url)
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", p.query, p.fragment))


async def fetch_once(url, max_chars):
    """Returns a page, None (permanent: robots.txt says no, 4xx, 429 "slow down", not HTML) or "retry" (timeout, network error, 5xx)."""
    try:
        hit = await safe_fetch(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9"}, timeout=20, polite=True)
        if not hit:
            return None
        res, final_url = hit
        if res.status_code >= 500:
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
