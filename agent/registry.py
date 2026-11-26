"""The company's legal entity and owners from GLEIF, the official global register of Legal Entity Identifiers (open data,
free API, no key). Used only to add owners and parents to the "own group" filter and to show the registered name; research
never waits on it for long and never fails because of it. A match must be unambiguous, or nothing is returned."""
import asyncio
import re
from urllib.parse import quote

from . import web
from .db import recorded

API = "https://api.gleif.org/api/v1"
TIMEOUT = 10  # seconds for the whole lookup; a slow register must not slow research
LEGAL = r"(private|pvt|limited|ltd|llp|llc|inc|incorporated|corp|corporation|company|co|plc|gmbh|ag|sa|sas|bv|nv|pte|pty|pbc|the)"
# A registered name may add one of these to the brand ("Juspay Technologies"); a business line ("PayU Finance") may not.
FILLER = {"software", "technologies", "technology", "tech", "solutions", "systems", "labs", "india", "infotech"}
COUNTRIES = {"india": "IN", "united states": "US", "usa": "US", "us": "US", "united kingdom": "GB", "uk": "GB", "ireland": "IE",
             "singapore": "SG", "germany": "DE", "france": "FR", "netherlands": "NL", "canada": "CA", "australia": "AU",
             "united arab emirates": "AE", "uae": "AE", "japan": "JP"}


def core(name):
    """A company name without punctuation, legal suffixes or a bracketed alias: "Zoho Corporation Pvt. Ltd." -> "zoho"."""
    words = re.sub(r"[^a-z0-9]+", " ", re.sub(r"\(.*?\)", " ", str(name or "").lower())).split()
    return " ".join(w for w in words if not re.fullmatch(LEGAL, w))


def country_of(region):
    text = f" {re.sub(r'[^a-z]+', ' ', str(region or '').lower())} "
    return next((code for word, code in COUNTRIES.items() if f" {word} " in text), None)


def best_match(records, name, region=""):
    """The one register entry that is this company, or None. Exact brand first, then brand + one filler word;
    a country named in `region` breaks ties; anything still ambiguous is no match (a wrong owner is worse than none)."""
    want = core(name)
    if not want:
        return None
    exact, close = [], []
    for rec in records:
        e = (rec.get("attributes") or {}).get("entity") or {}
        if e.get("status") != "ACTIVE":
            continue
        got = core((e.get("legalName") or {}).get("name"))
        rest = got[len(want) + 1:].split() if got.startswith(f"{want} ") else []
        if got == want:
            exact.append(rec)
        elif len(rest) == 1 and rest[0] in FILLER:
            close.append(rec)
    home = country_of(region)

    def local(group):
        return [r for r in group if home and r["attributes"]["entity"]["legalAddress"].get("country") == home]
    # An entity in the company's own country beats a foreign holding company ("Razorpay Software" in India over "Razorpay, Inc.").
    for pick in (local(exact), local(close), exact, close):
        if len(pick) == 1:
            return pick[0]
        if pick:
            return None  # several equally good entries: can't tell which
    return None


async def _get(path, params=None):
    async def go():
        res = await web.http("GET", f"{API}{path}" + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else ""),
                             headers={"User-Agent": web.UA, "Accept": "application/vnd.api+json"}, timeout=6)
        if res.status_code == 404:
            return {}
        if not res.is_success:
            raise RuntimeError(f"GLEIF returned {res.status_code}")
        return res.json()
    return await recorded("gleif", "registry", {"path": path, "params": params}, go, lambda raw: {"cost": 0})


async def _lookup(name, region):
    found = await _get("/lei-records", {"filter[fulltext]": quote(core(name)), "page[size]": "15"})
    rec = best_match(found.get("data") or [], name, region)
    if not rec:
        return None
    lei = rec["id"]
    direct, ultimate = await asyncio.gather(_get(f"/lei-records/{lei}/direct-parent"), _get(f"/lei-records/{lei}/ultimate-parent"))

    def owner(d):
        return (((d.get("data") or {}).get("attributes") or {}).get("entity") or {}).get("legalName", {}).get("name") or None
    e = rec["attributes"]["entity"]
    return {"lei": lei, "legal_name": e["legalName"]["name"], "country": e["legalAddress"].get("country"),
            "parent": owner(direct), "ultimate_parent": owner(ultimate), "source": "GLEIF"}


async def legal_entity(name, region=""):
    """{"lei", "legal_name", "country", "parent", "ultimate_parent", "source"} or None. Never raises, never waits over TIMEOUT."""
    try:
        return await asyncio.wait_for(_lookup(name, region), TIMEOUT)
    except Exception as error:  # noqa: BLE001 - timeout or register down: research goes on without it
        web.warn(f"GLEIF lookup skipped for {name}: {str(error)[:120] or type(error).__name__}")
        return None


def owners(entity):
    """Names to treat as the company's own group: its registered name and its parents."""
    if not entity:
        return []
    return [n for n in (entity["legal_name"], entity["parent"], entity["ultimate_parent"]) if n]
