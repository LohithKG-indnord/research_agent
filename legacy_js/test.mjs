import test from "node:test";
import assert from "node:assert/strict";
import { scoreCompany, parseCompanies, passages, checkFinancials, identify } from "./agent/pipeline.mjs";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { openDb, dbFile, saveResult, syncDb, withJob, recorded } from "./agent/db.mjs";
import { reviewCompany, enrichCompany, saveLead } from "./agent/people.mjs";
import { sameSite, safeFetch, isPrivate, search, engines } from "./agent/web.mjs";

test("only verbatim quotes count; unsupported dimensions are capped", () => {
  const sources = [
    { id: "S1", url: "https://acme.com", type: "company_site", text: "Acme builds AI-powered helpdesk software for mid-market retailers across Europe." },
    { id: "S2", url: "https://news.example/acme", type: "third_party", text: "Acme raised a $20M Series B to expand into North America, March 2026." }
  ];
  const q = {
    scores: { problem_fit: 80, profile_fit: 70, timing: 85, role_clarity: 60 },
    evidence: [
      { source_id: "S1", quote: "AI-powered helpdesk software for mid-market retailers", supports: "profile_fit", claim: "" },
      { source_id: "S2", quote: "raised a $20M Series B to expand into North America", supports: "timing", claim: "" },
      { source_id: "S1", quote: "Acme struggles with fraud on every transaction", supports: "problem_fit", claim: "" } // fabricated
    ]
  };
  const s = scoreCompany(q, sources, "acme.com");
  assert.deepEqual(s.evidence.map(e => e.status), ["verified", "verified", "inferred"]);
  assert.equal(s.scores.profile_fit, 70);
  assert.equal(s.scores.timing, 85);
  assert.equal(s.scores.problem_fit, 35); // only fabricated support -> capped
  assert.equal(s.scores.role_clarity, 35);
  assert.equal(s.meets_min_evidence, true);
  assert.ok(s.final > 0 && s.final <= 100);
  assert.ok(scoreCompany(q, sources, "acme.com", "low").final < s.final);
});

test("parses company lines with optional domains", () => {
  assert.deepEqual(parseCompanies("Zoho\nRazorpay, razorpay.com\nAcme, Inc. | https://acme.io; Tata Consultancy Services"), [
    { name: "Zoho", website: "" },
    { name: "Razorpay", website: "razorpay.com" },
    { name: "Acme, Inc.", website: "https://acme.io" },
    { name: "Tata Consultancy Services", website: "" }
  ]);
});

test("passages split long text without losing it", () => {
  const text = "One sentence here. ".repeat(100) + "x".repeat(2000);
  const parts = passages(text);
  assert.ok(parts.length > 3 && parts.every(p => p.length <= 1050));
  assert.ok(parts.join("").replace(/\s/g, "").length >= text.replace(/\s/g, "").length - 50);
});

test("a missing or look-alike domain is never treated as the company's own site", () => {
  assert.equal(sameSite("news.example", ""), false);
  assert.equal(sameSite("notzoho.com", "zoho.com"), false);
  assert.equal(sameSite("careers.zoho.com", "zoho.com"), true);
});

test("a redirect to a private or social host is not followed", async t => {
  const visited = [];
  t.mock.method(globalThis, "fetch", async url => {
    visited.push(url);
    return new Response(null, { status: 302, headers: { location: visited.length === 1 ? "http://127.0.0.1/admin" : "https://www.linkedin.com/x" } });
  });
  assert.equal(await safeFetch("https://1.1.1.1/", {}), null);
  assert.equal(await safeFetch("https://1.1.1.1/", {}), null);
  assert.deepEqual(visited, ["https://1.1.1.1/", "https://1.1.1.1/"]);
  assert.ok(isPrivate("::1") && isPrivate("::ffff:10.0.0.1") && !isPrivate("8.8.8.8"));
});

test("DonSeTch runs first; Exa is paid for only when DonSeTch finds too little about the company", async t => {
  process.env.EXA_API_KEY = "test";
  process.env.DONSETCH_BINARY = "does-not-exist";
  let status = 200;
  const exaCalls = t.mock.method(globalThis, "fetch", async () => new Response(JSON.stringify({ results: [
    { title: "Acme A", url: "https://a.com/x", highlights: ["one", "two"], publishedDate: "2026-01-02" }, { title: "L", url: "https://linkedin.com/x" }
  ], costDollars: { total: 0.007 } }), { status }));
  // DonSeTch broken: Exa's results are mapped and used.
  assert.deepEqual(await search("q"), { rows: [{ title: "Acme A", url: "https://a.com/x", snippet: "one two", date: "2026-01-02" }], provider: "exa", cost: 0.007 });
  status = 402;
  assert.equal((await search("q")).provider, "none");
  status = 200;
  const row = (title, url) => ({ title, url, snippet: "", date: "" });
  // Enough DonSeTch results about Acme: Exa is not called.
  t.mock.method(engines, "donsetch", async () => ({ rows: [row("Acme news", "https://n.com/1"), row("About Acme Inc", "https://acme.com"), row("Other", "https://o.com")], cost: 0 }));
  const before = exaCalls.mock.callCount();
  assert.deepEqual(await search("q", 4, "Acme Inc"), { rows: [row("Acme news", "https://n.com/1"), row("About Acme Inc", "https://acme.com"), row("Other", "https://o.com")], provider: "donsetch", cost: 0 });
  assert.equal(exaCalls.mock.callCount(), before);
  // Too little about Acme: Exa tops up, results about Acme first, duplicates dropped.
  const r = await search("q", 8, "Acme");
  assert.equal(r.provider, "donsetch+exa");
  assert.equal(r.cost, 0.007);
  assert.deepEqual(r.rows.map(x => x.url), ["https://n.com/1", "https://acme.com", "https://a.com/x", "https://o.com"]);
  // Financial searches always add Exa, its results first, even when DonSeTch is enough.
  const money = await search("q", 4, "Acme Inc", true);
  assert.equal(money.provider, "donsetch+exa");
  assert.deepEqual(money.rows.map(x => x.url), ["https://a.com/x", "https://n.com/1", "https://acme.com", "https://o.com"]);
});

test("financial figures need the exact value, the right metric and the right company", () => {
  const sources = [
    { id: "S1", url: "https://n.example/z", title: "Zoho results", text: "Zoho reports Rs 12,313 Cr revenue and Rs 3,191 Cr profit in FY25. It has 18,000 employees." },
    { id: "S2", url: "https://n.example/o", title: "Other Corp", text: "Other Corp posted revenue of $500 million in FY25 after a strong year." },
    { id: "S3", url: "https://n.example/a", title: "Zoho", text: "Zoho adjusted revenue increased 48.9% to INR 24,315 Crores last year." }
  ];
  const f = (metric, value, source_id, quote, period = "FY2025") => ({ metric, value, period, basis: "reported", entity: "Zoho", source_id, quote });
  const good = checkFinancials([
    f("revenue", "Rs 2,313 Cr", "S1", "Zoho reports Rs 12,313 Cr revenue"), // not the whole number
    f("revenue", "Rs 12,313 million", "S1", "Zoho reports Rs 12,313 Cr revenue"), // wrong unit
    f("revenue", "$500 million", "S2", "posted revenue of $500 million in FY25"), // other company
    f("revenue", "Rs 12,313 Cr", "S1", "Zoho reports Rs 12,313 Cr revenue"),
    f("profit", "18,000", "S1", "It has 18,000 employees"), // wrong metric
    f("employees", "18,000", "S1", "It has 18,000 employees", "FY2031")
  ], sources, "Zoho Corporation");
  assert.equal(good.revenue.value, "Rs 12,313 Cr");
  assert.equal(good.revenue.period, "FY2025");
  assert.equal(good.profit, null);
  assert.equal(good.employees.period, "Unknown"); // year not in the source
  assert.equal(good.funding, null);
  const rival = { id: "K", url: "https://n.example/k", title: "Kayako rivals", text: "Kayako competes with Freshdesk, which has 210 employees in Chennai." };
  assert.equal(checkFinancials([{ ...f("employees", "210", "K", "Freshdesk, which has 210 employees"), entity: "Freshdesk" }], [rival], "Kayako").employees, null);
  const clash = [{ id: "A", url: "https://a.example", title: "", text: "Swiggy annual revenue is Rs 8,887 Cr for FY26." }, { id: "B", url: "https://b.example", title: "", text: "Swiggy revenue was ~Rs 23,053 Cr in FY26." }];
  const both = checkFinancials([f("revenue", "Rs 23,053 Cr", "B", "Swiggy revenue was ~Rs 23,053 Cr in FY26", "FY26"), f("revenue", "Rs 8,887 Cr", "A", "Swiggy annual revenue is Rs 8,887 Cr for FY26", "FY26")].map(x => ({ ...x, entity: "Swiggy" })), clash, "Swiggy Limited").revenue;
  assert.equal(both.basis, "estimate");
  const wiki = [{ id: "W", url: "https://w.example", title: "Swiggy", text: "Revenue ₹ 163.33 billion[3] (US$1.7 billion) (2025). Swiggy Q2 revenue rose to Rs 7,571 crore." }];
  const annual = checkFinancials([f("revenue", "Rs 7,571 crore", "W", "Swiggy Q2 revenue rose to Rs 7,571 crore", "Q2 FY27"), f("revenue", "₹163.33 billion", "W", "Revenue ₹163.33 billion (US$1.7 billion) (2025)", "2025")].map(x => ({ ...x, entity: "Swiggy" })), wiki, "Swiggy").revenue;
  assert.equal(annual.value, "₹163.33 billion");
  assert.equal(annual.others[0].period, "Unknown");
  assert.deepEqual(both.others.map(o => o.value), ["Rs 8,887 Cr"]);
  assert.equal(checkFinancials([f("revenue", "INR 24,315 Crores", "S3", "adjusted revenue increased 48.9% to INR 24,315 Crores")], sources, "Zoho").revenue.basis, "adjusted");
  assert.equal(checkFinancials([f("growth", "48.9%", "S3", "adjusted revenue increased 48.9% to INR 24,315 Crores")], sources, "Zoho").growth.value, "48.9%");
});

test("no search results and no website stops before any Claude call", async t => {
  delete process.env.EXA_API_KEY;
  process.env.DONSETCH_BINARY = "does-not-exist";
  const calls = t.mock.method(globalThis, "fetch", async () => { throw new Error("no network expected"); });
  await assert.rejects(identify({ product: "x", criteria: "" }, { name: "Acme", website: "" }), /No search results/);
  assert.equal(calls.mock.callCount(), 0);
});

test("people are looked up only for accepted companies, and never paid for twice", async t => {
  const dir = await mkdtemp(join(tmpdir(), "leads-"));
  const run_id = "11111111-1111-1111-1111-111111111111";
  const result = { run: { run_id }, companies: [{ id: "c1", domain: "acme.com", people: [], review: { decision: null }, buyer_titles: ["VP Support", "COO"], buyer_departments: [] }] };
  for (const f of [`${run_id}.json`, "leads.json"]) await writeFile(join(dir, f), JSON.stringify(result));
  process.env.HUNTER_API_KEY = "h"; process.env.APOLLO_API_KEY = "a";
  const urls = [];
  t.mock.method(globalThis, "fetch", async (url, o) => {
    urls.push(String(url));
    if (String(url).includes("hunter")) return new Response(JSON.stringify({ data: { emails: [{ value: "jo@acme.com", first_name: "Jo", last_name: "Li", position: "VP Support", provenance: "found", confidence: 92, verification: { status: "valid" } }] } }));
    if (String(url).includes("api_search")) return new Response(JSON.stringify({ people: [{ id: "3", has_email: true, title: "Engineer" }, { id: "1", has_email: true, title: "coo" }, { id: "2", has_email: false, title: "COO" }] }));
    assert.deepEqual(JSON.parse(o.body), { details: [{ id: "1" }, { id: "3" }] }); // buyer title first
    return new Response(JSON.stringify({ credits_consumed: 1, matches: [{ id: "1", name: "Al Ray", title: "COO", email: "al@acme.com", email_status: "verified" }, null] }));
  });
  const ask = extra => ({ run_id, company_id: "c1", ...extra });

  await assert.rejects(enrichCompany(ask({ provider: "hunter" }), dir), /Accept the company/);
  await reviewCompany(ask({ decision: "rejected" }), dir);
  await assert.rejects(enrichCompany(ask({ provider: "hunter" }), dir), /Accept the company/);
  await assert.rejects(reviewCompany(ask({ decision: "accepted", run_id: "../../secret" }), dir), /Unknown run/);
  assert.equal(urls.length, 0);

  await reviewCompany(ask({ decision: "accepted" }), dir); // looks up the buyer titles with the first provider
  assert.equal(urls.length, 1);
  assert.match(urls[0], /domain=acme.com&type=personal&required_field=full_name%2Cposition&limit=5&offset=0&job_titles=VP\+Support%2CCOO/);
  const company = await enrichCompany(ask({ provider: "apollo" }), dir);
  assert.deepEqual(company.people.map(p => [p.name, p.email, p.status, p.source]), [["Jo Li", "jo@acme.com", "verified", "hunter"], ["Al Ray", "al@acme.com", "verified", "apollo"]]);
  assert.equal(JSON.parse(await readFile(join(dir, "leads.json"), "utf8")).companies[0].people.length, 2);
  assert.equal((await saveLead(ask({ person_id: "hunter:jo@acme.com", saved: true }), dir)).people[0].saved, true);
  await assert.rejects(saveLead(ask({ person_id: "nobody", saved: true }), dir), /Unknown lead/);
  await assert.rejects(saveLead(ask({ person_id: "hunter:jo@acme.com", saved: "yes" }), dir), /true or false/);
  // No Claude key: the lead stays accepted and the reason the message is missing is kept; rejecting writes nothing.
  delete process.env.ANTHROPIC_API_KEY; delete process.env.CLAUDE_API_KEY;
  const al = await saveLead(ask({ person_id: "apollo:1", saved: true }), dir);
  const ray = al.people.find(p => p.id === "apollo:1");
  assert.equal(ray.saved, true);
  assert.match(ray.message_error, /ANTHROPIC_API_KEY/);
  assert.equal((await saveLead(ask({ person_id: "apollo:1", saved: false }), dir)).people.find(p => p.id === "apollo:1").saved, false);

  delete process.env.HUNTER_API_KEY; // Apollo free plan: the decision is kept and the reason shown
  t.mock.method(globalThis, "fetch", async () => new Response(JSON.stringify({ error: "not included", error_code: "API_INACCESSIBLE" }), { status: 403 }));
  result.companies[0].people = [];
  await writeFile(join(dir, `${run_id}.json`), JSON.stringify(result));
  const blocked = await reviewCompany(ask({ decision: "accepted" }), dir);
  assert.equal(blocked.review.decision, "accepted");
  assert.match(blocked.people_error, /free plan does not include its people API/);
});

test("a result is stored once per company, with decisions, history and raw responses", async () => {
  const dir = await mkdtemp(join(tmpdir(), "db-"));
  const db = openDb(dbFile(dir));
  const all = sql => db.prepare(sql).all().map(r => ({ ...r }));
  const count = t => all(`SELECT count(*) n FROM ${t}`)[0].n;
  const make = (n, at, industry) => ({
    run: { run_id: `${n}`.repeat(8) + "-1111-1111-1111-111111111111", mode: "named_companies", model_version: "m", prompt_version: "p", rerank_model: "r", duration_s: 5 },
    campaign: { title: "t", generated_at: at, icp: { product: "x", criteria: "" } }, counts: { requested: 1, researched: 1, failed: 0 }, failed: [],
    companies: [{ id: `${n}c`, input_name: "Acme", name: "Acme", domain: "acme.com", website: "https://acme.com", industry, size_band: "201-500", location: "IN", summary: "s", status: "shortlisted", fit_score: 60, score_breakdown: { problem_fit: 50 }, tier: "Qualified",
      evidence: [{ url: "https://acme.com", quote: "q", claim: "c", supports: "timing", source_type: "company_site", status: "verified", fetched_at: at }],
      buying_signals: [{ signal: "hiring", date: "", url: null }], meets_min_evidence: true, people: [], review: { decision: null },
      financials: { revenue: { value: "Rs 5 Cr", period: "FY25", basis: "reported", entity: "Acme", url: "https://n.example", quote: "q", others: [] }, profit: null } }]
  });
  const first = make(1, "2026-01-01T00:00:00.000Z", "SaaS"), second = make(2, "2026-02-01T00:00:00.000Z", "Fintech");
  saveResult(db, first); saveResult(db, second); saveResult(db, first); saveResult(db, second);
  assert.equal(count("companies"), 1);
  assert.equal(count("company_rankings"), 2);
  assert.equal(count("evidence"), 2);
  assert.deepEqual(all("SELECT industry, employee_count_min a, employee_count_max b, first_seen f FROM companies"), [{ industry: "Fintech", a: 201, b: 500, f: "2026-01-01T00:00:00.000Z" }]);
  assert.deepEqual(all("SELECT field, old_value, new_value FROM enrichment_history"), [{ field: "industry", old_value: "SaaS", new_value: "Fintech" }]);
  assert.equal(all("SELECT value FROM company_financials WHERE is_primary = 1").length, 2);
  assert.equal(count("leads"), 0);

  const decide = (decision, decided_at) => { second.companies[0].review = { decision, reason: null, decided_by: "local user", decided_at }; saveResult(db, second); };
  decide("accepted", "2026-02-02T00:00:00.000Z");
  const person = { id: "hunter:a", name: "Jo", title: "VP", email: "Jo@acme.com", status: "verified", source: "hunter", checked_at: "2026-02-02T00:00:00.000Z" };
  second.companies[0].people = [{ ...person, saved: true }, { ...person, id: "apollo:1", email: "jo@acme.com", source: "apollo", saved: true }, { ...person, id: "hunter:b", name: "Not saved", email: "ns@acme.com" }];
  decide("rejected", "2026-02-03T00:00:00.000Z");
  assert.deepEqual(all("SELECT action, previous_status p FROM review_decisions ORDER BY decided_at"), [{ action: "accepted", p: "review_required" }, { action: "rejected", p: "accepted" }]);
  assert.deepEqual(all("SELECT status FROM leads"), [{ status: "rejected" }]);
  assert.equal(count("people"), 1); // same email from two providers is one person
  assert.equal(count("person_sources"), 2);
  second.companies[0].people.forEach(p => { p.saved = false; }); saveResult(db, second); // unsaving removes the lead
  assert.equal(count("people") + count("company_people") + count("person_sources"), 0);

  await withJob(dir, "job-without-result", () => recorded("exa", "search", { query: "q" }, async () => ({ ok: 1 }), () => ({ cost: 0.007 })));
  await assert.rejects(withJob(dir, "job-without-result", () => recorded("exa", "search", {}, async () => { throw new Error("402"); })), /402/);
  assert.deepEqual(all("SELECT status, cost_estimate c, error_code e FROM provider_runs"), [{ status: "ok", c: 0.007, e: null }, { status: "error", c: null, e: "402" }]);
  assert.equal(count("provider_responses"), 1);

  const broken = make(3, "2026-03-01T00:00:00.000Z", "x"); broken.companies[0].evidence[0].quote = null;
  assert.throws(() => saveResult(db, broken));
  assert.equal(all("SELECT id FROM search_jobs WHERE id LIKE '33333333%'").length, 0); // rolled back

  await writeFile(join(dir, `${second.run.run_id}.json`), JSON.stringify(second));
  assert.equal(await syncDb(dir), 1);
  assert.equal(count("companies"), 1);
});

test("Hunter tries buyer titles, then senior people in the buyer department, then a few decision makers", async t => {
  const dir = await mkdtemp(join(tmpdir(), "hunter-"));
  const run_id = "22222222-2222-2222-2222-222222222222";
  const result = { run: { run_id }, companies: [{ id: "c1", domain: "acme.com", people: [], review: { decision: null }, buyer_titles: ["Head of Support"], buyer_departments: ["support"] }] };
  await writeFile(join(dir, `${run_id}.json`), JSON.stringify(result));
  process.env.HUNTER_API_KEY = "h";
  const urls = [];
  t.mock.method(globalThis, "fetch", async url => {
    urls.push(String(url));
    const emails = /job_titles|department/.test(String(url)) ? [] : [{ value: "ceo@acme.com", first_name: "Sam", last_name: "Ko", position: "CEO", verification: { status: "valid" } }];
    return new Response(JSON.stringify({ data: { emails } }));
  });
  const company = await reviewCompany({ run_id, company_id: "c1", decision: "accepted" }, dir);
  assert.equal(urls.length, 3);
  assert.match(urls[1], /limit=5&offset=0&department=support&seniority=executive%2Csenior/);
  assert.match(urls[2], /limit=3&offset=0&seniority=executive&decision_maker=true/);
  assert.deepEqual(company.people.map(p => [p.name, p.reason]), [["Sam Ko", "Decision maker (no one found with the buyer titles)"]]);
});

test("finding more leads pages through Hunter and never returns anyone seen before, even from an earlier run", async t => {
  const dir = await mkdtemp(join(tmpdir(), "more-"));
  const run_id = "33333333-3333-3333-3333-333333333333", old_run = "44444444-4444-4444-4444-444444444444";
  const company = { id: "c1", domain: "acme.com", people: [], review: { decision: null }, buyer_titles: ["VP Support"], buyer_departments: [] };
  await writeFile(join(dir, `${run_id}.json`), JSON.stringify({ run: { run_id }, companies: [company] }));
  await writeFile(join(dir, `${old_run}.json`), JSON.stringify({ run: { run_id: old_run }, companies: [{ ...company, id: "old", people: [{ id: "hunter:p0@acme.com", name: "Old Lead", email: "p0@acme.com", saved: false }] }] }));
  process.env.HUNTER_API_KEY = "h";
  const everyone = Array.from({ length: 7 }, (_, i) => ({ value: `p${i}@acme.com`, first_name: "P", last_name: String(i), position: "VP Support" }));
  const urls = [];
  t.mock.method(globalThis, "fetch", async url => {
    urls.push(String(url));
    const q = new URL(url).searchParams;
    return new Response(JSON.stringify({ data: { emails: everyone.slice(+q.get("offset"), +q.get("offset") + +q.get("limit")) } }));
  });
  const first = await reviewCompany({ run_id, company_id: "c1", decision: "accepted" }, dir);
  const names = c => c.people.filter(p => p.batch === Math.max(...c.people.map(x => x.batch))).map(p => p.email);
  assert.deepEqual(first.people.find(p => p.from_run)?.saved, false); // earlier run's lead comes back with its decision
  assert.deepEqual(names(first), ["p1@acme.com", "p2@acme.com", "p3@acme.com", "p4@acme.com", "p5@acme.com"]); // p0 skipped, the next page fills the 5th
  const more = await enrichCompany({ run_id, company_id: "c1", provider: "hunter" }, dir);
  assert.deepEqual(names(more), ["p6@acme.com"]);
  const none = await enrichCompany({ run_id, company_id: "c1", provider: "hunter" }, dir);
  assert.equal(none.people_lookups.at(-1).found, 0);
  assert.equal(urls.length, 3); // no search once Hunter has no one left
});

test("figures under the parent's name count, and growth can be calculated from two revenue figures", () => {
  const src = [{ id: "S1", url: "https://n.example/e", title: "Eternal results", text: "Eternal reported revenue of ₹54,364 crore in FY26 (the year to March 2026), against ₹20,243 crore in FY25, with a net profit of ₹366 crore." }];
  const item = (metric, value, quote, entity = "Eternal Limited (Zomato parent) consolidated") => ({ metric, value, period: "FY2026", basis: "reported", entity, source_id: "S1", quote });
  const quote = "Eternal reported revenue of ₹54,364 crore in FY26 (the year to March 2026), against ₹20,243 crore in FY25";
  const f = checkFinancials([item("revenue", "₹54,364 crore", quote), item("profit", "₹366 crore", "with a net profit of ₹366 crore"), item("growth", "+169%", quote)], src, "Zomato (Eternal Limited)");
  assert.equal(f.revenue.value, "₹54,364 crore");
  assert.equal(f.profit.value, "₹366 crore");
  assert.deepEqual([f.growth.value, f.growth.basis], ["+168.6%", "calculated"]);
  // A growth figure that doesn't match the sum, or a company with no such parent name, is still rejected.
  assert.equal(checkFinancials([item("growth", "+120%", quote)], src, "Zomato (Eternal Limited)").growth, null);
  assert.equal(checkFinancials([item("revenue", "₹54,364 crore", quote)], src, "Zomato").revenue, null);
});
