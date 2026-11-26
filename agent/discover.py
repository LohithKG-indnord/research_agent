"""Find companies to research from an ideal-customer description ("SaaS companies in Bengaluru with 50-500 staff").
Two web searches find list pages ("top SaaS companies in Bengaluru"), one Exa search limited to company websites finds
the companies themselves, and one Claude call picks the 5 best matches. Every pick must be named in what was found."""
import os
import re

from .llm import ask_json
from .pipeline import STR, UNTRUSTED, _gather, _unique_by_url, norm, obj
from .web import domain_of, exa_search, fetch_page, map_limit, search

PICKS = 5
PICK_SCHEMA = obj({"companies": {"type": "array", "items": obj({"name": STR, "domain": STR, "why": STR, "source_id": STR})}})


async def _exa_companies(icp):
    if not os.environ.get("EXA_API_KEY"):
        return {"rows": [], "cost": 0}
    try:
        return await exa_search(icp, 20, category="company")
    except Exception:  # noqa: BLE001 - the list searches still answer
        return {"rows": [], "cost": 0}


async def find_companies(body):
    """{"icp": text, "exclude": [names already shown], "product": optional} -> {"companies": [...], "usd"}"""
    icp = re.sub(r"\s+", " ", str(body.get("icp") or "")).strip()
    if len(icp) < 5:
        raise ValueError("Describe the companies you're looking for, e.g. SaaS companies in Bengaluru with 50-500 staff.")
    if len(icp) > 1000:
        raise ValueError("Keep the description under 1,000 characters.")
    exclude = [str(x) for x in body.get("exclude") or []][:50]
    lists = await _gather(search(f"{icp} list of companies", 8), search(f"top {icp}", 8), _exa_companies(icp))
    rows = _unique_by_url([r for found in lists for r in found["rows"]])[:30]
    # The best list pages name far more companies than their snippet shows: read the top three.
    read = await map_limit([r for r in rows if r in lists[0]["rows"][:2] + lists[1]["rows"][:1]], 3, lambda r: fetch_page(r["url"], 6000))
    full = {p["url"]: p["text"] for p in read if p}
    sources = [{"id": f"S{i + 1}", "url": r["url"], "title": r["title"], "text": f"{r['snippet']} {full.get(r['url'], '')}".strip()} for i, r in enumerate(rows)]
    usd = sum(found["cost"] or 0 for found in lists)
    if not sources:
        return {"companies": [], "usd": usd}
    got = await ask_json(
        system=f"""You find prospective customers. From the numbered search results, pick the {PICKS} real companies that best match the ideal-customer description. {UNTRUSTED}
Rules: only companies named in the results; never a list site, directory, publisher, investor or job board itself; never one in the exclude list; spread across different companies, no duplicates or subsidiaries of each other.
name: the company's common name. domain: its own website domain (bare, like "example.com") only if it appears in the results, else empty. why: one line on how it matches each part of the description (industry, location, size, model), saying which parts are unconfirmed. source_id: the result that names it.
Return fewer than {PICKS} if fewer really match; never pad with weak matches.""",
        prompt=f"""Ideal customer: {icp}
{f"Seller's product (context only): {body.get('product')}" if body.get("product") else ""}
Exclude: {", ".join(exclude) or "none"}

Results:
{chr(10).join(f"[{s['id']}] {s['url']} | {s['title'][:90]}{chr(10)}{s['text'][:2500]}" for s in sources)}""",
        schema=PICK_SCHEMA, effort="medium", max_tokens=2000)
    by_id = {s["id"]: s for s in sources}
    everything = " ".join(f"{s['url']} {s['text']}" for s in sources).lower()
    picks, seen = [], {norm(x) for x in exclude}
    for c in got["data"]["companies"]:
        src = by_id.get(c["source_id"])
        key = norm(c["name"])
        # Named in the cited result, not shown before, and the domain kept only if the results contain it.
        if not src or not key or key in seen or f" {key} " not in f" {norm(src['title'] + ' ' + src['text'])} ":
            continue
        seen.add(key)
        domain = domain_of(f"https://{c['domain'].strip().lower()}") if c["domain"].strip() else ""
        picks.append({"name": c["name"].strip(), "domain": domain if domain and domain in everything else "",
                      "why": c["why"], "source": src["url"]})
    return {"companies": picks[:PICKS], "usd": round(usd + (got["usage"]["usd"] or 0), 4)}
