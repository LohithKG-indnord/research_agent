"""Buying roles: role-based inference of where each lead sits in the buying committee, from their title and the company
research (no LinkedIn reading, no extra searches about the person). These are starting hypotheses, not confirmed roles:
confidence says how much is known, and "high" needs public quotes that name the person."""
import re

from .llm import ask_json
from .pipeline import STR, STR_ARR, UNTRUSTED, obj
from .util import now_iso

ROLES = ["Initiator", "Influencer", "Champion", "Decision Maker", "Economic Buyer", "Buyer / Procurement", "Gatekeeper", "End User"]
LEVELS = ["high", "medium", "low"]
BUDGET = ["likely", "unknown", "unlikely"]
INTERESTS = ["cost", "productivity", "growth", "technology", "risk"]
# The 100-point authority / influence model: weight of each 0-4 score.
WEIGHTS = {"budget_ownership": 25, "decision_authority": 25, "functional_ownership": 20, "influence": 20, "advocacy": 10}

SCORES = obj({k: {"type": "integer"} for k in WEIGHTS})
SCHEMA = obj({
    "buying_motion": STR, "category": STR, "why_motion": STR,
    "people": {"type": "array", "items": obj({
        "person_id": STR, "primary_role": STR, "secondary_role": STR,
        "decision_authority": STR, "technical_influence": STR, "budget_ownership": STR, "business_interest": STR,
        "scores": SCORES, "confidence": STR, "entry_point": {"type": "boolean"}, "why": STR, "known": STR_ARR, "assumed": STR_ARR})},
})

GUIDE = """Buying roles (use these exact names):
Initiator - spots a problem or opportunity and starts the buying process (Engineering Manager, IT Director, Product Head, Business Head).
Influencer - shapes requirements, evaluation criteria and vendor preferences (Enterprise Architect, CTO office, CISO, Head of Data).
Champion - advocates the solution and drives internal alignment (VP Engineering, GCC Director, Transformation Head).
Decision Maker - makes or shares the final vendor decision (CIO, CTO, GCC Head, VP Engineering).
Economic Buyer - controls the budget and approves the spend (CIO, CFO, CTO, Business Unit Head).
Buyer / Procurement - commercials, contracts, negotiation, onboarding (Procurement Head, Strategic Sourcing, Vendor Management).
Gatekeeper - controls access, information and vendor entry (Vendor Management, PMO, Executive Assistant, CISO for security sign-off).
End User - uses the service or technology and shapes adoption (developers, QA, data engineers, operations teams).
Title cheat sheet (starting hypotheses only): CIO/CTO/CFO/GCC Head -> Economic Buyer or Decision Maker; VP Engineering/VP Technology -> Decision Maker or Champion; Director of Engineering/Director IT -> Champion or Initiator; Enterprise/Chief Architect -> Influencer; Head of Cloud/Data/AI -> Influencer or Decision Maker; Head of Procurement/Strategic Sourcing -> Buyer / Procurement; Vendor Management Head -> Buyer / Procurement or Gatekeeper; Program Director/Transformation Head -> Champion or Initiator; CISO/Head of Information Security -> Influencer or Gatekeeper.
Buying motion: "IT Software" when the seller sells a product or SaaS (the buyer asks: does it solve the problem, integrate, stay secure, what does it cost to own, will people adopt it); "IT Services" when it sells consulting, engineering or managed services (the buyer asks: can this partner deliver, with the right expertise, within budget, alongside our teams). In services buying, engineering and delivery leaders and procurement weigh more; in software buying, architects and security weigh more."""


def score(scores):
    """The 0-100 authority / influence score from five 0-4 scores."""
    return round(sum(WEIGHTS[k] * min(4, max(0, int(scores.get(k) or 0))) / 4 for k in WEIGHTS))


def named_in(person, evidence):
    """Verified quotes from the research that name this person: the only public signals beyond their title."""
    name = re.sub(r"[^a-z ]+", " ", str(person.get("name") or "").lower()).split()
    if len(name) < 2:
        return []
    return [e["quote"] for e in evidence or [] if e.get("status") == "verified"
            and all(re.search(rf"\b{re.escape(w)}\b", str(e.get("quote") or "").lower()) for w in (name[0], name[-1]))]


def _pick(value, allowed, default):
    v = str(value or "").strip()
    return next((a for a in allowed if a.lower() == v.lower()), default)


def tidy(p, person, evidence):
    """Keep the model's answer inside the framework, and cap confidence by what is actually known."""
    signals = named_in(person, evidence)
    confidence = _pick(p.get("confidence"), LEVELS, "low")
    if confidence == "high" and len(signals) < 2:
        confidence = "medium"  # high needs several public signals, not just a title and company context
    primary = _pick(p.get("primary_role"), ROLES, "Initiator")
    secondary = _pick(p.get("secondary_role"), ROLES, "")
    scores = {k: min(4, max(0, int((p.get("scores") or {}).get(k) or 0))) for k in WEIGHTS}
    return {"primary_role": primary, "secondary_role": "" if secondary == primary else secondary,
            "decision_authority": _pick(p.get("decision_authority"), LEVELS, "low"), "technical_influence": _pick(p.get("technical_influence"), LEVELS, "low"),
            "budget_ownership": _pick(p.get("budget_ownership"), BUDGET, "unknown"), "business_interest": _pick(p.get("business_interest"), INTERESTS, "productivity"),
            "scores": scores, "score": score(scores), "confidence": confidence, "entry_point": bool(p.get("entry_point")),
            "why": str(p.get("why") or "")[:400], "known": [str(x)[:200] for x in p.get("known") or []][:5],
            "assumed": [str(x)[:200] for x in p.get("assumed") or []][:5], "public_signals": signals[:3]}


async def classify(company, result):
    """One Claude call for all of a company's leads. Returns the company's buying_committee."""
    people = company.get("people") or []
    if not people:
        raise ValueError("Find leads first: there is no one to classify.")
    icp = (result.get("campaign") or {}).get("icp") or {}
    evidence = company.get("evidence") or []
    leads = "\n".join(f"- id {p['id']}: {p.get('name')}, {p.get('title') or 'title unknown'}"
                      + (f" | public quotes naming them: {' / '.join(named_in(p, evidence)[:3])}" if named_in(p, evidence) else "") for p in people)
    got = await ask_json(
        system=f"""You map a company's buying committee for a B2B sales team using role-based inference from secondary data. {UNTRUSTED}
{GUIDE}
For each person: primary_role and secondary_role (or empty) from the roles above; decision_authority, technical_influence: high/medium/low; budget_ownership: likely/unknown/unlikely; business_interest: the one of cost/productivity/growth/technology/risk they are most likely measured on.
scores, each 0-4 (0 no evidence, 4 strong evidence): budget_ownership (budget, P&L, investment responsibility), decision_authority (approves vendors, technology or strategy), functional_ownership (owns the function the seller's offering serves), influence (shapes standards, architecture or requirements), advocacy (visibility and ability to rally stakeholders). Score only what the title and company facts support; a title alone rarely earns 4.
confidence: low when it rests mainly on the title; medium when the title plus company facts (size, structure, initiatives) support it but authority is unverified; high only when several public quotes about the person support it.
known: facts you were given (title, company facts). assumed: what you inferred. Never present an inference as known; never call anyone a confirmed Decision Maker.
entry_point: true for the 1-2 people who most likely own the operational problem the seller solves - the best way into the account even without final budget authority.
why: one sentence on the classification.
buying_motion: "IT Software" or "IT Services" for the seller's offering; category: e.g. Application Development, Cloud, Data, AI, Managed Services, a named product; why_motion: one sentence.""",
        prompt=f"""Seller's offering: {icp.get('product') or 'not specified'}
Company: {company.get('name')} ({company.get('domain')}) - {company.get('industry') or ''}, {company.get('size_band') or ''}, {company.get('location') or ''}
{company.get('summary') or ''}
Buying signals: {'; '.join(b.get('signal', '') for b in (company.get('buying_signals') or [])[:4]) or 'none found'}

Leads:
{leads}""",
        schema=SCHEMA, effort="low", max_tokens=6000)
    data = got["data"]
    by_id = {p["id"]: p for p in people}
    out = {}
    for p in data.get("people") or []:
        if p.get("person_id") in by_id:
            out[p["person_id"]] = tidy(p, by_id[p["person_id"]], evidence)
    return {"buying_motion": _pick(data.get("buying_motion"), ["IT Software", "IT Services"], "IT Services"), "category": str(data.get("category") or "")[:80],
            "why_motion": str(data.get("why_motion") or "")[:300], "people": out, "classified_at": now_iso(), "usd": got["usage"]["usd"]}


async def buying_roles(body, directory=None):
    """{"run_id", "company_id", "again"?} -> the company, with buying_committee for all its leads (one Claude call, about $0.02-0.03)."""
    from .people import change, LEADS_DIR

    async def fn(company, result, _where):
        done = company.get("buying_committee") or {}
        missing = [p for p in company.get("people") or [] if p["id"] not in (done.get("people") or {})]
        if done and not missing and not body.get("again"):
            return
        company["buying_committee"] = await classify(company, result)
    return await change(directory or LEADS_DIR, body.get("run_id"), body.get("company_id"), fn)
