"""SQLite storage, following the design doc's tables (sections 8 and 8.2). The JSON result file stays the
primary contract (8.6); save_result projects it into the tables and can be repeated safely."""
import contextvars
import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .util import now_iso, to_json

# One organisation per installation (doc Q8). Both ids are on every row so rows can move to Flow unchanged.
ORG = "00000000-0000-4000-8000-000000000001"
PROJECT = "00000000-0000-4000-8000-000000000002"
USER = "00000000-0000-4000-8000-000000000003"

SCOPE = "organization_id TEXT NOT NULL REFERENCES workspaces(id), project_id TEXT NOT NULL"


def _json_col(column):
    return f"{column} TEXT CHECK ({column} IS NULL OR json_valid({column}))"


SCHEMA = f"""
-- Application shell
CREATE TABLE IF NOT EXISTS workspaces (id TEXT PRIMARY KEY, name TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, name TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS workspace_members (workspace_id TEXT NOT NULL REFERENCES workspaces(id), user_id TEXT NOT NULL REFERENCES users(id), role TEXT NOT NULL, PRIMARY KEY (workspace_id, user_id)) STRICT;

-- Search and providers
CREATE TABLE IF NOT EXISTS research_briefs (id TEXT PRIMARY KEY, {SCOPE}, product_description TEXT, criteria TEXT, {_json_col("companies")}, target_industries TEXT, geographies TEXT, size_range TEXT, buying_signals TEXT, exclusions TEXT, target_roles TEXT, version INTEGER NOT NULL DEFAULT 1) STRICT;
CREATE TABLE IF NOT EXISTS search_jobs (id TEXT PRIMARY KEY, {SCOPE}, brief_id TEXT REFERENCES research_briefs(id), mode TEXT, status TEXT NOT NULL, top_k INTEGER, model_version TEXT, prompt_version TEXT, rerank_model TEXT, {_json_col("errors")}, created_at TEXT, completed_at TEXT) STRICT;
CREATE TABLE IF NOT EXISTS provider_runs (id INTEGER PRIMARY KEY, {SCOPE}, search_job_id TEXT NOT NULL REFERENCES search_jobs(id), stage TEXT NOT NULL, provider TEXT NOT NULL, request_hash TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL, latency_ms INTEGER, cost_estimate REAL, input_tokens INTEGER, output_tokens INTEGER, error_code TEXT, started_at TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS provider_responses (provider_run_id INTEGER PRIMARY KEY REFERENCES provider_runs(id) ON DELETE CASCADE, {SCOPE}, {_json_col("request")}, payload TEXT NOT NULL CHECK (json_valid(payload)), retrieved_at TEXT NOT NULL) STRICT;

-- Company and people
CREATE TABLE IF NOT EXISTS companies (id TEXT PRIMARY KEY, {SCOPE}, name TEXT NOT NULL, domain TEXT, website TEXT, linkedin_url TEXT, description TEXT, industry TEXT, location TEXT, size_band TEXT, employee_count_min INTEGER, employee_count_max INTEGER, first_seen TEXT NOT NULL, last_checked TEXT NOT NULL, UNIQUE (organization_id, domain)) STRICT;
CREATE INDEX IF NOT EXISTS companies_filter ON companies (industry, location, employee_count_min);
CREATE TABLE IF NOT EXISTS people (id TEXT PRIMARY KEY, {SCOPE}, provider_id TEXT, name TEXT NOT NULL, location TEXT, linkedin_url TEXT, email TEXT, email_status TEXT, confidence TEXT, status TEXT NOT NULL, last_checked TEXT NOT NULL) STRICT;
CREATE INDEX IF NOT EXISTS people_email ON people (organization_id, email);
CREATE INDEX IF NOT EXISTS people_linkedin ON people (organization_id, linkedin_url);
CREATE TABLE IF NOT EXISTS company_people (company_id TEXT NOT NULL REFERENCES companies(id), person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE, {SCOPE}, title TEXT, band TEXT, function TEXT, role TEXT, reason TEXT, is_current INTEGER, PRIMARY KEY (company_id, person_id)) STRICT;

-- Qualification and ranking
CREATE TABLE IF NOT EXISTS company_rankings (id TEXT PRIMARY KEY, {SCOPE}, search_job_id TEXT NOT NULL REFERENCES search_jobs(id), company_id TEXT NOT NULL REFERENCES companies(id), input_name TEXT, identity_confidence TEXT, status TEXT, rule_score REAL, rerank_score REAL, relevance_score REAL, buying_intent_score REAL, product_fit_score REAL, final_score REAL, rank_position INTEGER, tier TEXT, willingness_to_buy TEXT, {_json_col("score_breakdown")}, explanation TEXT, pitch_angle TEXT, {_json_col("details")}, {_json_col("evidence_ids")}, meets_min_evidence INTEGER, model_version TEXT, provider TEXT, cost_usd REAL, scored_at TEXT NOT NULL) STRICT;
CREATE INDEX IF NOT EXISTS rankings_top ON company_rankings (search_job_id, final_score DESC);
CREATE TABLE IF NOT EXISTS person_rankings (id TEXT PRIMARY KEY, {SCOPE}, search_job_id TEXT NOT NULL REFERENCES search_jobs(id), person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE, final_score REAL, rank_position INTEGER, explanation TEXT, scored_at TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS buying_signals (id TEXT PRIMARY KEY, {SCOPE}, ranking_id TEXT NOT NULL REFERENCES company_rankings(id), company_id TEXT NOT NULL REFERENCES companies(id), signal TEXT NOT NULL, signal_date TEXT, url TEXT) STRICT;

-- Sources and evidence
CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, {SCOPE}, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, ranking_id TEXT REFERENCES company_rankings(id), url TEXT, title TEXT, excerpt TEXT NOT NULL, claim TEXT, supports TEXT, source_type TEXT, status TEXT NOT NULL, published_at TEXT, captured_at TEXT, provider TEXT, hash TEXT NOT NULL) STRICT;
CREATE INDEX IF NOT EXISTS evidence_entity ON evidence (entity_type, entity_id);
CREATE TABLE IF NOT EXISTS company_sources (company_id TEXT NOT NULL REFERENCES companies(id), url TEXT NOT NULL, {SCOPE}, source_type TEXT, last_retrieved TEXT, PRIMARY KEY (company_id, url)) STRICT;
CREATE TABLE IF NOT EXISTS person_sources (person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE, provider TEXT NOT NULL, {SCOPE}, source_url TEXT, retrieved_at TEXT NOT NULL, confidence TEXT, {_json_col("fields")}, purpose TEXT NOT NULL, retention_class TEXT NOT NULL, delete_after TEXT, PRIMARY KEY (person_id, provider)) STRICT;
-- Added to the doc's schema: financial figures
CREATE TABLE IF NOT EXISTS company_financials (id TEXT PRIMARY KEY, {SCOPE}, ranking_id TEXT NOT NULL REFERENCES company_rankings(id), company_id TEXT NOT NULL REFERENCES companies(id), metric TEXT NOT NULL, value TEXT NOT NULL, period TEXT, basis TEXT, entity TEXT, is_primary INTEGER NOT NULL, quote TEXT, url TEXT, captured_at TEXT) STRICT;

-- Lead history
CREATE TABLE IF NOT EXISTS review_decisions (id TEXT PRIMARY KEY, {SCOPE}, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, search_job_id TEXT NOT NULL REFERENCES search_jobs(id), reviewer TEXT NOT NULL, action TEXT NOT NULL, reason TEXT, decided_at TEXT NOT NULL, previous_status TEXT) STRICT;
CREATE TABLE IF NOT EXISTS leads (id TEXT PRIMARY KEY, {SCOPE}, company_id TEXT NOT NULL REFERENCES companies(id), ranking_id TEXT NOT NULL REFERENCES company_rankings(id), status TEXT NOT NULL, accepted_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE (organization_id, company_id)) STRICT;
CREATE TABLE IF NOT EXISTS enrichment_history (id TEXT PRIMARY KEY, {SCOPE}, company_id TEXT NOT NULL REFERENCES companies(id), search_job_id TEXT NOT NULL REFERENCES search_jobs(id), kind TEXT NOT NULL, provider TEXT, field TEXT, old_value TEXT, new_value TEXT, recorded_at TEXT NOT NULL) STRICT;
"""

_open = {}


def open_db(file):
    file = str(file)
    if file in _open:
        return _open[file]
    Path(file).parent.mkdir(parents=True, exist_ok=True)
    # Autocommit; save_result opens its own transaction. One connection is shared by the server's threads.
    db = sqlite3.connect(file, isolation_level=None, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 5000")
    db.executescript(SCHEMA)
    db.execute("INSERT OR IGNORE INTO workspaces VALUES (?, 'Default organisation')", (ORG,))
    db.execute("INSERT OR IGNORE INTO users VALUES (?, 'local user')", (USER,))
    db.execute("INSERT OR IGNORE INTO workspace_members VALUES (?, ?, 'owner')", (ORG, USER))
    _open[file] = db
    return db


def db_file(directory):
    return str(Path(directory) / "leads.db")


def sha(*parts):
    return hashlib.sha256(to_json(list(parts)).encode("utf-8")).hexdigest()


def uid(*parts):
    """A stable UUID from content, so saving the same result twice updates rows and never duplicates them."""
    return re.sub(r"^(.{8})(.{4}).(.{3}).(.{3})(.{12}).*$", r"\1-\2-5\3-8\4-\5", sha(*parts))


def plain(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (dict, list)):
        return to_json(v)
    return v


def put(db, table, row, key="id"):
    """Insert a row, or update it when the key already exists. Table and column names only ever come from this file."""
    full = {**row, "organization_id": ORG, "project_id": PROJECT}
    cols = list(full)
    keys = key.split(",")
    update = [f"{c} = excluded.{c}" for c in cols if c not in keys]
    db.execute(
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)}) ON CONFLICT ({key}) DO UPDATE SET {','.join(update)}",
        [plain(full[c]) for c in cols],
    )


def add_job(db, job_id):
    db.execute("INSERT OR IGNORE INTO search_jobs (id, organization_id, project_id, status, created_at) VALUES (?, ?, ?, 'running', ?)", (job_id, ORG, PROJECT, now_iso()))


def job_status(r):
    counts = r["counts"]
    if counts["researched"] + counts["failed"] < counts["requested"]:
        return "running"
    if not r["companies"]:
        return "needs_attention"
    accepted = [c for c in r["companies"] if (c.get("review") or {}).get("decision") == "accepted"]
    if any(c.get("people") for c in accepted):
        return "enriched"
    if accepted:
        return "approved"
    return "rejected" if all((c.get("review") or {}).get("decision") == "rejected" for c in r["companies"]) else "review_required"


def employees(band):
    text = str(band or "").replace(",", "")
    n = [int(m) for m in re.findall(r"\d+", text)]
    if re.match(r"^\D*\d+\s*(-|–|to)\s*\d+", text):
        return n[0], n[1]
    return (n[0], None) if re.match(r"^\D*\d+\s*\+", text) else (None, None)


def _iso_minus(at, seconds):
    t = datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp() - seconds
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def save_company(db, r, c, at):
    known = db.execute("SELECT * FROM companies WHERE organization_id = ? AND domain = ?", (ORG, c["domain"])).fetchone() if c.get("domain") else None
    company_id = known["id"] if known else (uid(ORG, c["domain"]) if c.get("domain") else c["id"])  # a name alone is never a key (doc 8.4)
    lo, hi = employees(c.get("size_band"))
    fields = {"name": c.get("name"), "website": c.get("website"), "description": c.get("summary"), "industry": c.get("industry"), "location": c.get("location"), "size_band": c.get("size_band")}
    # An older run being saved again must not overwrite what a newer run found.
    if not known or at >= known["last_checked"]:
        for field, value in (fields.items() if known else []):
            if known[field] != plain(value):
                put(db, "enrichment_history", {"id": uid("field", company_id, r["run"]["run_id"], field), "company_id": company_id, "search_job_id": r["run"]["run_id"], "kind": "field_change", "field": field, "old_value": known[field], "new_value": value, "recorded_at": at})
        put(db, "companies", {"id": company_id, **fields, "domain": c.get("domain") or None, "employee_count_min": lo, "employee_count_max": hi, "first_seen": known["first_seen"] if known else at, "last_checked": at})
    evidence = [{"id": uid(c["id"], e.get("url"), e.get("quote"), e.get("supports")), "e": e} for e in c["evidence"]]
    usage = c.get("usage") or {}
    details = {k: c.get(k) for k in ("fit_reasons", "objections", "buying_functions", "buyer_titles", "buyer_departments", "next_steps", "unknowns", "usage", "pages_read", "growth_business", "growth_technical", "growth_play", "growth_levers", "product_solutions", "product_extras", "growth_written", "buying_capacity", "chat", "path_to_win", "user_notes", "peers", "case_study", "case_study_search", "business_type", "related_companies", "similar_companies")}
    put(db, "company_rankings", {
        "id": c["id"], "search_job_id": r["run"]["run_id"], "company_id": company_id, "input_name": c.get("input_name"), "identity_confidence": c.get("identity_confidence"), "status": c.get("status"),
        "relevance_score": c.get("relevance_score"), "buying_intent_score": c.get("buying_intent_score"), "product_fit_score": c["score_breakdown"].get("problem_fit"), "final_score": c.get("fit_score"), "rank_position": c.get("rank_position"),
        "tier": c.get("tier"), "willingness_to_buy": c.get("willingness_to_buy"), "score_breakdown": c["score_breakdown"], "explanation": c.get("verdict"), "pitch_angle": c.get("pitch_angle"),
        "details": {k: v for k, v in details.items() if k in c},"evidence_ids": [x["id"] for x in evidence],
        "meets_min_evidence": c.get("meets_min_evidence"), "model_version": r["run"].get("model_version"), "provider": usage.get("search_provider"), "cost_usd": usage.get("usd"), "scored_at": at,
    })

    def source(url, kind, when):
        if url:
            put(db, "company_sources", {"company_id": company_id, "url": url, "source_type": kind, "last_retrieved": when}, "company_id,url")

    for x in evidence:
        e = x["e"]
        put(db, "evidence", {"id": x["id"], "entity_type": "company", "entity_id": company_id, "ranking_id": c["id"], "url": e.get("url"), "excerpt": e.get("quote"), "claim": e.get("claim"), "supports": e.get("supports"), "source_type": e.get("source_type"), "status": e.get("status"), "captured_at": e.get("fetched_at"), "hash": sha(e.get("url"), e.get("quote"))})
        source(e.get("url"), e.get("source_type"), e.get("fetched_at"))
    for b in c.get("buying_signals") or []:
        put(db, "buying_signals", {"id": uid("signal", c["id"], b.get("signal"), b.get("url")), "ranking_id": c["id"], "company_id": company_id, "signal": b.get("signal"), "signal_date": b.get("date") or None, "url": b.get("url")})
    for metric, f in (c.get("financials") or {}).items():
        for i, x in enumerate([f, *(f.get("others") or [])] if f else []):
            put(db, "company_financials", {"id": uid("fin", c["id"], metric, x.get("value"), x.get("url")), "ranking_id": c["id"], "company_id": company_id, "metric": metric, "value": x.get("value"), "period": x.get("period"), "basis": x.get("basis"), "entity": x.get("entity"), "is_primary": i == 0, "quote": x.get("quote"), "url": x.get("url"), "captured_at": x.get("fetched_at")})
            source(x.get("url"), "financial", x.get("fetched_at"))
    review = c.get("review") or {}
    if review.get("decision"):
        rid = uid("review", c["id"], review.get("decided_at"))
        before = db.execute("SELECT action FROM review_decisions WHERE entity_id = ? AND id <> ? AND decided_at < ? ORDER BY decided_at DESC LIMIT 1", (c["id"], rid, review.get("decided_at"))).fetchone()
        put(db, "review_decisions", {"id": rid, "entity_type": "company_ranking", "entity_id": c["id"], "search_job_id": r["run"]["run_id"], "reviewer": review.get("decided_by"), "action": review["decision"], "reason": review.get("reason"), "decided_at": review.get("decided_at"), "previous_status": before["action"] if before else "review_required"})
        # The latest decision about a company wins, whichever run it was made in.
        lead = db.execute("SELECT * FROM leads WHERE organization_id = ? AND company_id = ?", (ORG, company_id)).fetchone()
        if (review["decided_at"] >= lead["updated_at"]) if lead else review["decision"] == "accepted":
            put(db, "leads", {"id": lead["id"] if lead else uid("lead", company_id), "company_id": company_id, "ranking_id": c["id"], "status": review["decision"], "accepted_at": review["decided_at"] if review["decision"] == "accepted" else lead["accepted_at"], "updated_at": review["decided_at"]})
    # Only leads the user accepted are stored; rejecting one removes it (and its sources) unless another company still links to it.
    person_key = lambda p: uid("person", (p.get("email") or "").lower() or p.get("profile_url") or p.get("id"))
    for p in [p for p in c.get("people") or [] if p.get("saved") is False]:
        person = person_key(p)
        db.execute("DELETE FROM company_people WHERE company_id = ? AND person_id = ?", (company_id, person))
        db.execute("DELETE FROM people WHERE id = ? AND NOT EXISTS (SELECT 1 FROM company_people WHERE person_id = ?)", (person, person))
    for p in [p for p in c.get("people") or [] if p.get("saved")]:
        person = person_key(p)
        confidence = None if p.get("confidence") is None else _js_string(p["confidence"])
        put(db, "people", {"id": person, "provider_id": p.get("id"), "name": p.get("name"), "linkedin_url": p.get("profile_url") if re.search(r"linkedin\.com", p.get("profile_url") or "", re.I) else None, "email": p.get("email"), "email_status": p.get("email_status"), "confidence": confidence, "status": p.get("status"), "last_checked": p.get("checked_at")})
        put(db, "company_people", {"company_id": company_id, "person_id": person, "title": p.get("title"), "band": p.get("band"), "function": p.get("function"), "role": p.get("role"), "reason": p.get("reason"), "is_current": 1}, "company_id,person_id")
        put(db, "person_sources", {"person_id": person, "provider": p.get("source"), "source_url": p.get("profile_url"), "retrieved_at": p.get("checked_at"), "confidence": confidence, "fields": [k for k in ("name", "title", "email", "profile_url") if p.get(k)], "purpose": "B2B sales research for an accepted company", "retention_class": "unset"}, "person_id,provider")
    for lk in c.get("people_lookups") or []:
        put(db, "enrichment_history", {"id": uid("lookup", c["id"], lk.get("provider"), lk.get("titles")), "company_id": company_id, "search_job_id": r["run"]["run_id"], "kind": "people_lookup", "provider": lk.get("provider"), "new_value": {"titles": lk.get("titles"), "found": lk.get("found"), "credits": lk.get("credits")}, "recorded_at": lk.get("checked_at")})


def _js_string(v):
    """String(v) as JavaScript writes it: 92 not 92.0."""
    return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)


def save_result(db, r):
    """Project one result file into the tables, all or nothing."""
    at = r["campaign"]["generated_at"]
    icp = r["campaign"]["icp"]
    brief = uid("brief", icp, r["campaign"]["title"])
    db.execute("BEGIN IMMEDIATE")
    try:
        put(db, "research_briefs", {"id": brief, "product_description": icp.get("product"), "criteria": icp.get("criteria"), "companies": [c.get("input_name") for c in [*r["companies"], *r["failed"]]]})
        status = job_status(r)
        add_job(db, r["run"]["run_id"])
        put(db, "search_jobs", {"id": r["run"]["run_id"], "brief_id": brief, "mode": r["run"].get("mode"), "status": status, "model_version": r["run"].get("model_version"), "prompt_version": r["run"].get("prompt_version"), "rerank_model": r["run"].get("rerank_model"), "errors": r["failed"], "created_at": _iso_minus(at, r["run"]["duration_s"]), "completed_at": None if status == "running" else at})
        for c in r["companies"]:
            save_company(db, r, c, at)
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def try_save(directory, result):
    """Save without ever failing the caller: the JSON file already holds the result, and sync_db retries at the next start."""
    try:
        save_result(open_db(db_file(directory)), result)
        return True
    except Exception as error:  # noqa: BLE001 - the result is safe in the JSON file
        print(f"Database save failed (result is safe in the JSON file): {error}")
        return False


RESULT_FILE = re.compile(r"^[0-9a-f-]{36}\.json$")


def sync_db(directory):
    """Project every named-company result file, oldest first. Returns how many were saved."""
    results = []
    for f in sorted(Path(directory).glob("*.json")) if Path(directory).is_dir() else []:
        if RESULT_FILE.match(f.name):
            try:
                results.append(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
    named = sorted((r for r in results if (r.get("run") or {}).get("mode") == "named_companies"), key=lambda r: r["campaign"]["generated_at"])
    return sum(1 for r in named if try_save(directory, r))


# ---------- Raw provider responses (doc 8.1) ----------
_context = contextvars.ContextVar("job", default=None)


async def with_job(directory, job_id, fn):
    """Run `fn()` (a coroutine function) with provider calls recorded under this job; tasks it starts inherit the job."""
    token = _context.set({"dir": directory, "job_id": job_id})
    try:
        return await fn()
    finally:
        _context.reset(token)


async def recorded(provider, stage, request, fn, meta=lambda raw: {}):
    """Run a provider call and record it under the current job. `meta(raw)` may return cost and tokens."""
    ctx = _context.get()
    started = time.time()

    def log(status, raw=None, error=None, ok=False):
        if not ctx:
            return
        try:
            db = open_db(db_file(ctx["dir"]))
            add_job(db, ctx["job_id"])
            m = meta(raw) if ok else {}
            at = datetime.fromtimestamp(started, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            cur = db.execute(
                "INSERT INTO provider_runs (organization_id, project_id, search_job_id, stage, provider, request_hash, status, latency_ms, cost_estimate, input_tokens, output_tokens, error_code, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ORG, PROJECT, ctx["job_id"], stage, provider, sha(request), status, int((time.time() - started) * 1000), m.get("cost"), m.get("input_tokens"), m.get("output_tokens"), str(error)[:300] if error else None, at),
            )
            # Claude prompts repeat page text already held as evidence, so only their hash is kept.
            if ok:
                db.execute("INSERT INTO provider_responses VALUES (?, ?, ?, ?, ?, ?)", (cur.lastrowid, ORG, PROJECT, None if provider == "anthropic" else to_json(request), to_json(raw), at))
        except Exception as e:  # noqa: BLE001 - logging must never break a run
            print(f"Provider log failed: {e}")

    try:
        raw = await fn()
    except Exception as error:
        log("error", error=error)
        raise
    log("ok", raw, ok=True)
    return raw
