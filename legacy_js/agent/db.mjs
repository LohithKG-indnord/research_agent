// SQLite storage, following the design doc's tables (sections 8 and 8.2). The JSON result file stays the
// primary contract (8.6); saveResult projects it into the tables and can be repeated safely.
import { DatabaseSync } from "node:sqlite";
import { AsyncLocalStorage } from "node:async_hooks";
import { createHash } from "node:crypto";
import { mkdirSync } from "node:fs";
import { readdir, readFile } from "node:fs/promises";
import { dirname, join } from "node:path";

// One organisation per installation (doc Q8). Both ids are on every row so rows can move to Flow unchanged.
const ORG = "00000000-0000-4000-8000-000000000001";
const PROJECT = "00000000-0000-4000-8000-000000000002";
const USER = "00000000-0000-4000-8000-000000000003";

const scope = "organization_id TEXT NOT NULL REFERENCES workspaces(id), project_id TEXT NOT NULL";
const json = column => `${column} TEXT CHECK (${column} IS NULL OR json_valid(${column}))`;
const SCHEMA = `
-- Application shell
CREATE TABLE IF NOT EXISTS workspaces (id TEXT PRIMARY KEY, name TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, name TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS workspace_members (workspace_id TEXT NOT NULL REFERENCES workspaces(id), user_id TEXT NOT NULL REFERENCES users(id), role TEXT NOT NULL, PRIMARY KEY (workspace_id, user_id)) STRICT;

-- Search and providers
CREATE TABLE IF NOT EXISTS research_briefs (id TEXT PRIMARY KEY, ${scope}, product_description TEXT, criteria TEXT, ${json("companies")}, target_industries TEXT, geographies TEXT, size_range TEXT, buying_signals TEXT, exclusions TEXT, target_roles TEXT, version INTEGER NOT NULL DEFAULT 1) STRICT;
CREATE TABLE IF NOT EXISTS search_jobs (id TEXT PRIMARY KEY, ${scope}, brief_id TEXT REFERENCES research_briefs(id), mode TEXT, status TEXT NOT NULL, top_k INTEGER, model_version TEXT, prompt_version TEXT, rerank_model TEXT, ${json("errors")}, created_at TEXT, completed_at TEXT) STRICT;
CREATE TABLE IF NOT EXISTS provider_runs (id INTEGER PRIMARY KEY, ${scope}, search_job_id TEXT NOT NULL REFERENCES search_jobs(id), stage TEXT NOT NULL, provider TEXT NOT NULL, request_hash TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL, latency_ms INTEGER, cost_estimate REAL, input_tokens INTEGER, output_tokens INTEGER, error_code TEXT, started_at TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS provider_responses (provider_run_id INTEGER PRIMARY KEY REFERENCES provider_runs(id) ON DELETE CASCADE, ${scope}, ${json("request")}, payload TEXT NOT NULL CHECK (json_valid(payload)), retrieved_at TEXT NOT NULL) STRICT;

-- Company and people
CREATE TABLE IF NOT EXISTS companies (id TEXT PRIMARY KEY, ${scope}, name TEXT NOT NULL, domain TEXT, website TEXT, linkedin_url TEXT, description TEXT, industry TEXT, location TEXT, size_band TEXT, employee_count_min INTEGER, employee_count_max INTEGER, first_seen TEXT NOT NULL, last_checked TEXT NOT NULL, UNIQUE (organization_id, domain)) STRICT;
CREATE INDEX IF NOT EXISTS companies_filter ON companies (industry, location, employee_count_min);
CREATE TABLE IF NOT EXISTS people (id TEXT PRIMARY KEY, ${scope}, provider_id TEXT, name TEXT NOT NULL, location TEXT, linkedin_url TEXT, email TEXT, email_status TEXT, confidence TEXT, status TEXT NOT NULL, last_checked TEXT NOT NULL) STRICT;
CREATE INDEX IF NOT EXISTS people_email ON people (organization_id, email);
CREATE INDEX IF NOT EXISTS people_linkedin ON people (organization_id, linkedin_url);
CREATE TABLE IF NOT EXISTS company_people (company_id TEXT NOT NULL REFERENCES companies(id), person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE, ${scope}, title TEXT, band TEXT, function TEXT, role TEXT, reason TEXT, is_current INTEGER, PRIMARY KEY (company_id, person_id)) STRICT;

-- Qualification and ranking
CREATE TABLE IF NOT EXISTS company_rankings (id TEXT PRIMARY KEY, ${scope}, search_job_id TEXT NOT NULL REFERENCES search_jobs(id), company_id TEXT NOT NULL REFERENCES companies(id), input_name TEXT, identity_confidence TEXT, status TEXT, rule_score REAL, rerank_score REAL, relevance_score REAL, buying_intent_score REAL, product_fit_score REAL, final_score REAL, rank_position INTEGER, tier TEXT, willingness_to_buy TEXT, ${json("score_breakdown")}, explanation TEXT, pitch_angle TEXT, ${json("details")}, ${json("evidence_ids")}, meets_min_evidence INTEGER, model_version TEXT, provider TEXT, cost_usd REAL, scored_at TEXT NOT NULL) STRICT;
CREATE INDEX IF NOT EXISTS rankings_top ON company_rankings (search_job_id, final_score DESC);
CREATE TABLE IF NOT EXISTS person_rankings (id TEXT PRIMARY KEY, ${scope}, search_job_id TEXT NOT NULL REFERENCES search_jobs(id), person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE, final_score REAL, rank_position INTEGER, explanation TEXT, scored_at TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS buying_signals (id TEXT PRIMARY KEY, ${scope}, ranking_id TEXT NOT NULL REFERENCES company_rankings(id), company_id TEXT NOT NULL REFERENCES companies(id), signal TEXT NOT NULL, signal_date TEXT, url TEXT) STRICT;

-- Sources and evidence
CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, ${scope}, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, ranking_id TEXT REFERENCES company_rankings(id), url TEXT, title TEXT, excerpt TEXT NOT NULL, claim TEXT, supports TEXT, source_type TEXT, status TEXT NOT NULL, published_at TEXT, captured_at TEXT, provider TEXT, hash TEXT NOT NULL) STRICT;
CREATE INDEX IF NOT EXISTS evidence_entity ON evidence (entity_type, entity_id);
CREATE TABLE IF NOT EXISTS company_sources (company_id TEXT NOT NULL REFERENCES companies(id), url TEXT NOT NULL, ${scope}, source_type TEXT, last_retrieved TEXT, PRIMARY KEY (company_id, url)) STRICT;
CREATE TABLE IF NOT EXISTS person_sources (person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE, provider TEXT NOT NULL, ${scope}, source_url TEXT, retrieved_at TEXT NOT NULL, confidence TEXT, ${json("fields")}, purpose TEXT NOT NULL, retention_class TEXT NOT NULL, delete_after TEXT, PRIMARY KEY (person_id, provider)) STRICT;
-- Added to the doc's schema: financial figures
CREATE TABLE IF NOT EXISTS company_financials (id TEXT PRIMARY KEY, ${scope}, ranking_id TEXT NOT NULL REFERENCES company_rankings(id), company_id TEXT NOT NULL REFERENCES companies(id), metric TEXT NOT NULL, value TEXT NOT NULL, period TEXT, basis TEXT, entity TEXT, is_primary INTEGER NOT NULL, quote TEXT, url TEXT, captured_at TEXT) STRICT;

-- Lead history
CREATE TABLE IF NOT EXISTS review_decisions (id TEXT PRIMARY KEY, ${scope}, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, search_job_id TEXT NOT NULL REFERENCES search_jobs(id), reviewer TEXT NOT NULL, action TEXT NOT NULL, reason TEXT, decided_at TEXT NOT NULL, previous_status TEXT) STRICT;
CREATE TABLE IF NOT EXISTS leads (id TEXT PRIMARY KEY, ${scope}, company_id TEXT NOT NULL REFERENCES companies(id), ranking_id TEXT NOT NULL REFERENCES company_rankings(id), status TEXT NOT NULL, accepted_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE (organization_id, company_id)) STRICT;
CREATE TABLE IF NOT EXISTS enrichment_history (id TEXT PRIMARY KEY, ${scope}, company_id TEXT NOT NULL REFERENCES companies(id), search_job_id TEXT NOT NULL REFERENCES search_jobs(id), kind TEXT NOT NULL, provider TEXT, field TEXT, old_value TEXT, new_value TEXT, recorded_at TEXT NOT NULL) STRICT;
`;

const open = new Map();
export function openDb(file) {
  if (open.has(file)) return open.get(file);
  mkdirSync(dirname(file), { recursive: true });
  const db = new DatabaseSync(file);
  db.exec("PRAGMA journal_mode = WAL; PRAGMA foreign_keys = ON; PRAGMA busy_timeout = 5000;");
  db.exec(SCHEMA);
  db.prepare("INSERT OR IGNORE INTO workspaces VALUES (?, 'Default organisation')").run(ORG);
  db.prepare("INSERT OR IGNORE INTO users VALUES (?, 'local user')").run(USER);
  db.prepare("INSERT OR IGNORE INTO workspace_members VALUES (?, ?, 'owner')").run(ORG, USER);
  open.set(file, db);
  return db;
}
export const dbFile = dir => join(dir, "leads.db");

const sha = (...parts) => createHash("sha256").update(JSON.stringify(parts)).digest("hex");
// A stable UUID from content, so saving the same result twice updates rows and never duplicates them.
const uid = (...parts) => sha(...parts).replace(/^(.{8})(.{4}).(.{3}).(.{3})(.{12}).*$/, "$1-$2-5$3-8$4-$5");
const plain = v => v === undefined || v === null ? null : typeof v === "boolean" ? Number(v) : typeof v === "object" ? JSON.stringify(v) : v;

/** Insert a row, or update it when the key already exists. Table and column names only ever come from this file. */
function put(db, table, row, key = "id") {
  const full = { ...row, organization_id: ORG, project_id: PROJECT };
  const cols = Object.keys(full), keys = key.split(",");
  const update = cols.filter(c => !keys.includes(c)).map(c => `${c} = excluded.${c}`);
  db.prepare(`INSERT INTO ${table} (${cols.join()}) VALUES (${cols.map(() => "?").join()}) ON CONFLICT (${key}) DO UPDATE SET ${update.join()}`).run(...cols.map(c => plain(full[c])));
}

const addJob = (db, id) => db.prepare("INSERT OR IGNORE INTO search_jobs (id, organization_id, project_id, status, created_at) VALUES (?, ?, ?, 'running', ?)").run(id, ORG, PROJECT, new Date().toISOString());

function jobStatus(r) {
  if (r.counts.researched + r.counts.failed < r.counts.requested) return "running";
  if (!r.companies.length) return "needs_attention";
  const accepted = r.companies.filter(c => c.review?.decision === "accepted");
  if (accepted.some(c => c.people.length)) return "enriched";
  if (accepted.length) return "approved";
  return r.companies.every(c => c.review?.decision === "rejected") ? "rejected" : "review_required";
}

function employees(band) {
  const n = [...String(band || "").replace(/,/g, "").matchAll(/\d+/g)].map(m => Number(m[0]));
  if (/^\D*\d+\s*(-|–|to)\s*\d+/.test(String(band).replace(/,/g, ""))) return [n[0], n[1]];
  return /^\D*\d+\s*\+/.test(String(band).replace(/,/g, "")) ? [n[0], null] : [null, null];
}

function saveCompany(db, r, c, at) {
  const known = c.domain && db.prepare("SELECT * FROM companies WHERE organization_id = ? AND domain = ?").get(ORG, c.domain);
  const companyId = known?.id || (c.domain ? uid(ORG, c.domain) : c.id); // a name alone is never a key (doc 8.4)
  const [min, max] = employees(c.size_band);
  const fields = { name: c.name, website: c.website, description: c.summary, industry: c.industry, location: c.location, size_band: c.size_band };
  // An older run being saved again must not overwrite what a newer run found.
  if (!known || at >= known.last_checked) {
    for (const [field, value] of Object.entries(known ? fields : {})) {
      if (known[field] !== plain(value)) put(db, "enrichment_history", { id: uid("field", companyId, r.run.run_id, field), company_id: companyId, search_job_id: r.run.run_id, kind: "field_change", field, old_value: known[field], new_value: value, recorded_at: at });
    }
    put(db, "companies", { id: companyId, ...fields, domain: c.domain || null, employee_count_min: min, employee_count_max: max, first_seen: known?.first_seen || at, last_checked: at });
  }
  const evidence = c.evidence.map(e => ({ id: uid(c.id, e.url, e.quote, e.supports), e }));
  const { fit_reasons, objections, buying_functions, buyer_titles, buyer_departments, next_steps, unknowns, usage, pages_read } = c;
  put(db, "company_rankings", {
    id: c.id, search_job_id: r.run.run_id, company_id: companyId, input_name: c.input_name, identity_confidence: c.identity_confidence, status: c.status,
    relevance_score: c.relevance_score, buying_intent_score: c.buying_intent_score, product_fit_score: c.score_breakdown.problem_fit, final_score: c.fit_score, rank_position: c.rank_position,
    tier: c.tier, willingness_to_buy: c.willingness_to_buy, score_breakdown: c.score_breakdown, explanation: c.verdict, pitch_angle: c.pitch_angle,
    details: { fit_reasons, objections, buying_functions, buyer_titles, buyer_departments, next_steps, unknowns, usage, pages_read }, evidence_ids: evidence.map(x => x.id),
    meets_min_evidence: c.meets_min_evidence, model_version: r.run.model_version, provider: usage?.search_provider, cost_usd: usage?.usd, scored_at: at
  });
  const source = (url, type, when) => url && put(db, "company_sources", { company_id: companyId, url, source_type: type, last_retrieved: when }, "company_id,url");
  for (const { id, e } of evidence) {
    put(db, "evidence", { id, entity_type: "company", entity_id: companyId, ranking_id: c.id, url: e.url, excerpt: e.quote, claim: e.claim, supports: e.supports, source_type: e.source_type, status: e.status, captured_at: e.fetched_at, hash: sha(e.url, e.quote) });
    source(e.url, e.source_type, e.fetched_at);
  }
  for (const b of c.buying_signals) put(db, "buying_signals", { id: uid("signal", c.id, b.signal, b.url), ranking_id: c.id, company_id: companyId, signal: b.signal, signal_date: b.date || null, url: b.url });
  for (const [metric, f] of Object.entries(c.financials || {})) {
    for (const [i, x] of (f ? [f, ...(f.others ?? [])] : []).entries()) {
      put(db, "company_financials", { id: uid("fin", c.id, metric, x.value, x.url), ranking_id: c.id, company_id: companyId, metric, value: x.value, period: x.period, basis: x.basis, entity: x.entity, is_primary: i === 0, quote: x.quote, url: x.url, captured_at: x.fetched_at });
      source(x.url, "financial", x.fetched_at);
    }
  }
  if (c.review?.decision) {
    const id = uid("review", c.id, c.review.decided_at);
    const before = db.prepare("SELECT action FROM review_decisions WHERE entity_id = ? AND id <> ? AND decided_at < ? ORDER BY decided_at DESC LIMIT 1").get(c.id, id, c.review.decided_at);
    put(db, "review_decisions", { id, entity_type: "company_ranking", entity_id: c.id, search_job_id: r.run.run_id, reviewer: c.review.decided_by, action: c.review.decision, reason: c.review.reason, decided_at: c.review.decided_at, previous_status: before?.action || "review_required" });
    // The latest decision about a company wins, whichever run it was made in.
    const lead = db.prepare("SELECT * FROM leads WHERE organization_id = ? AND company_id = ?").get(ORG, companyId);
    if (lead ? c.review.decided_at >= lead.updated_at : c.review.decision === "accepted") {
      put(db, "leads", { id: lead?.id || uid("lead", companyId), company_id: companyId, ranking_id: c.id, status: c.review.decision, accepted_at: c.review.decision === "accepted" ? c.review.decided_at : lead.accepted_at, updated_at: c.review.decided_at });
    }
  }
  // Only leads the user saved are stored; unsaving one removes it (and its sources) unless another company still links to it.
  for (const p of c.people.filter(p => p.saved === false)) {
    const person = uid("person", p.email?.toLowerCase() || p.profile_url || p.id);
    db.prepare("DELETE FROM company_people WHERE company_id = ? AND person_id = ?").run(companyId, person);
    db.prepare("DELETE FROM people WHERE id = ? AND NOT EXISTS (SELECT 1 FROM company_people WHERE person_id = ?)").run(person, person);
  }
  for (const p of c.people.filter(p => p.saved)) {
    const person = uid("person", p.email?.toLowerCase() || p.profile_url || p.id);
    const confidence = p.confidence === null || p.confidence === undefined ? null : String(p.confidence);
    put(db, "people", { id: person, provider_id: p.id, name: p.name, linkedin_url: /linkedin\.com/i.test(p.profile_url || "") ? p.profile_url : null, email: p.email, email_status: p.email_status, confidence, status: p.status, last_checked: p.checked_at });
    put(db, "company_people", { company_id: companyId, person_id: person, title: p.title, band: p.band, function: p.function, role: p.role, reason: p.reason, is_current: 1 }, "company_id,person_id");
    put(db, "person_sources", { person_id: person, provider: p.source, source_url: p.profile_url, retrieved_at: p.checked_at, confidence, fields: ["name", "title", "email", "profile_url"].filter(k => p[k]), purpose: "B2B sales research for an accepted company", retention_class: "unset" }, "person_id,provider");
  }
  for (const l of c.people_lookups || []) put(db, "enrichment_history", { id: uid("lookup", c.id, l.provider, l.titles), company_id: companyId, search_job_id: r.run.run_id, kind: "people_lookup", provider: l.provider, new_value: { titles: l.titles, found: l.found, credits: l.credits }, recorded_at: l.checked_at });
}

/** Project one result file into the tables, all or nothing. */
export function saveResult(db, r) {
  const at = r.campaign.generated_at;
  const icp = r.campaign.icp;
  const brief = uid("brief", icp, r.campaign.title);
  db.exec("BEGIN IMMEDIATE");
  try {
    put(db, "research_briefs", { id: brief, product_description: icp.product, criteria: icp.criteria, companies: [...r.companies, ...r.failed].map(c => c.input_name) });
    const status = jobStatus(r);
    addJob(db, r.run.run_id);
    put(db, "search_jobs", { id: r.run.run_id, brief_id: brief, mode: r.run.mode, status, model_version: r.run.model_version, prompt_version: r.run.prompt_version, rerank_model: r.run.rerank_model, errors: r.failed, created_at: new Date(Date.parse(at) - r.run.duration_s * 1000).toISOString(), completed_at: status === "running" ? null : at });
    for (const c of r.companies) saveCompany(db, r, c, at);
    db.exec("COMMIT");
  } catch (error) { db.exec("ROLLBACK"); throw error; }
}

/** Save without ever failing the caller: the JSON file already holds the result, and syncDb retries at the next start. */
export function trySave(dir, result) {
  try { saveResult(openDb(dbFile(dir)), result); return true; } catch (error) { console.error(`Database save failed (result is safe in the JSON file): ${error.message}`); }
}

/** Project every named-company result file, oldest first. Returns how many were saved. */
export async function syncDb(dir) {
  const files = (await readdir(dir).catch(() => [])).filter(f => /^[0-9a-f-]{36}\.json$/.test(f));
  const results = await Promise.all(files.map(f => readFile(join(dir, f), "utf8").then(JSON.parse).catch(() => null)));
  const named = results.filter(r => r?.run?.mode === "named_companies").sort((a, b) => a.campaign.generated_at.localeCompare(b.campaign.generated_at));
  const saved = named.filter(r => trySave(dir, r));
  return saved.length;
}

// ---------- Raw provider responses (doc 8.1) ----------
const context = new AsyncLocalStorage();
export const withJob = (dir, jobId, fn) => context.run({ dir, jobId }, fn);

/** Run a provider call and record it under the current job. `meta(raw)` may return cost and tokens. */
export async function recorded(provider, stage, request, fn, meta = () => ({})) {
  const ctx = context.getStore();
  const started = Date.now();
  const log = (status, raw, error) => {
    if (!ctx) return;
    try {
      const db = openDb(dbFile(ctx.dir));
      addJob(db, ctx.jobId);
      const { cost = null, input_tokens = null, output_tokens = null } = raw === undefined ? {} : meta(raw);
      const at = new Date(started).toISOString();
      const { lastInsertRowid } = db.prepare("INSERT INTO provider_runs (organization_id, project_id, search_job_id, stage, provider, request_hash, status, latency_ms, cost_estimate, input_tokens, output_tokens, error_code, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
        .run(ORG, PROJECT, ctx.jobId, stage, provider, sha(request), status, Date.now() - started, cost, input_tokens, output_tokens, error ? String(error.message).slice(0, 300) : null, at);
      // Claude prompts repeat page text already held as evidence, so only their hash is kept.
      if (raw !== undefined) db.prepare("INSERT INTO provider_responses VALUES (?, ?, ?, ?, ?, ?)").run(lastInsertRowid, ORG, PROJECT, provider === "anthropic" ? null : JSON.stringify(request), JSON.stringify(raw), at);
    } catch (e) { console.error(`Provider log failed: ${e.message}`); }
  };
  try {
    const raw = await fn();
    log("ok", raw);
    return raw;
  } catch (error) { log("error", undefined, error); throw error; }
}
