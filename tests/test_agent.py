"""The same checks the JavaScript test.mjs ran, ported one for one. No test makes a real network or Claude call."""
import asyncio
import json
import re
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from agent import web
from agent.db import db_file, open_db, recorded, save_result, sync_db, with_job
from agent.people import enrich_company, review_company, save_lead
from agent.pipeline import check_financials, identify, parse_companies, passages, score_company
from agent.web import is_private, safe_fetch, same_site, search


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("EXA_API_KEY", "HUNTER_API_KEY", "APOLLO_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DONSETCH_BINARY", "does-not-exist")


def fake_http(monkeypatch, handler):
    """Replace every outside HTTP call; `handler(method, url, body)` returns an httpx.Response. Returns the list of URLs called."""
    calls = []

    async def http(method, url, *, headers=None, json_body=None, content=None, timeout=20):
        calls.append(url)
        return handler(method, url, json_body if json_body is not None else (json.loads(content) if content else None))
    monkeypatch.setattr(web, "http", http)
    return calls


def test_only_verbatim_quotes_count_and_unsupported_dimensions_are_capped():
    sources = [
        {"id": "S1", "url": "https://acme.com", "type": "company_site", "text": "Acme builds AI-powered helpdesk software for mid-market retailers across Europe."},
        {"id": "S2", "url": "https://news.example/acme", "type": "third_party", "text": "Acme raised a $20M Series B to expand into North America, March 2026."},
    ]
    q = {"scores": {"problem_fit": 80, "profile_fit": 70, "timing": 85, "role_clarity": 60}, "evidence": [
        {"source_id": "S1", "quote": "AI-powered helpdesk software for mid-market retailers", "supports": "profile_fit", "claim": ""},
        {"source_id": "S2", "quote": "raised a $20M Series B to expand into North America", "supports": "timing", "claim": ""},
        {"source_id": "S1", "quote": "Acme struggles with fraud on every transaction", "supports": "problem_fit", "claim": ""},  # fabricated
    ]}
    s = score_company(q, sources, "acme.com")
    assert [e["status"] for e in s["evidence"]] == ["verified", "verified", "inferred"]
    assert s["scores"]["profile_fit"] == 70
    assert s["scores"]["timing"] == 85
    assert s["scores"]["problem_fit"] == 35  # only fabricated support -> capped
    assert s["scores"]["role_clarity"] == 35
    assert s["meets_min_evidence"] is True
    assert 0 < s["final"] <= 100
    assert score_company(q, sources, "acme.com", "low")["final"] < s["final"]


def test_parses_company_lines_with_optional_domains():
    assert parse_companies("Zoho\nRazorpay, razorpay.com\nAcme, Inc. | https://acme.io; Tata Consultancy Services") == [
        {"name": "Zoho", "website": ""},
        {"name": "Razorpay", "website": "razorpay.com"},
        {"name": "Acme, Inc.", "website": "https://acme.io"},
        {"name": "Tata Consultancy Services", "website": ""},
    ]


def test_passages_split_long_text_without_losing_it():
    text = "One sentence here. " * 100 + "x" * 2000
    parts = passages(text)
    assert len(parts) > 3 and all(len(p) <= 1050 for p in parts)
    assert len(re.sub(r"\s", "", "".join(parts))) >= len(re.sub(r"\s", "", text)) - 50


def test_a_missing_or_look_alike_domain_is_never_the_companys_own_site():
    assert same_site("news.example", "") is False
    assert same_site("notzoho.com", "zoho.com") is False
    assert same_site("careers.zoho.com", "zoho.com") is True


def test_a_redirect_to_a_private_or_social_host_is_not_followed(monkeypatch):
    visited = fake_http(monkeypatch, lambda m, url, b: httpx.Response(302, headers={"location": "http://127.0.0.1/admin" if len(visited) == 1 else "https://www.linkedin.com/x"}))
    assert run(safe_fetch("https://1.1.1.1/")) is None
    assert run(safe_fetch("https://1.1.1.1/")) is None
    assert visited == ["https://1.1.1.1/", "https://1.1.1.1/"]
    assert is_private("::1") and is_private("::ffff:10.0.0.1") and not is_private("8.8.8.8")


def test_donsetch_runs_first_and_exa_is_paid_only_when_donsetch_finds_too_little(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test")
    status = {"code": 200}
    exa_calls = fake_http(monkeypatch, lambda m, url, b: httpx.Response(status["code"], json={"results": [
        {"title": "Acme A", "url": "https://a.com/x", "highlights": ["one", "two"], "publishedDate": "2026-01-02"}, {"title": "L", "url": "https://linkedin.com/x"},
    ], "costDollars": {"total": 0.007}}))
    # DonSeTch broken: Exa's results are mapped and used.
    assert run(search("q")) == {"rows": [{"title": "Acme A", "url": "https://a.com/x", "snippet": "one two", "date": "2026-01-02"}], "provider": "exa", "cost": 0.007}
    status["code"] = 402
    assert run(search("q"))["provider"] == "none"
    status["code"] = 200

    def row(title, url):
        return {"title": title, "url": url, "snippet": "", "date": ""}

    async def donsetch(query, most):
        return {"rows": [row("Acme news", "https://n.com/1"), row("About Acme Inc", "https://acme.com"), row("Other", "https://o.com")], "cost": 0}
    monkeypatch.setitem(web.engines, "donsetch", donsetch)
    before = len(exa_calls)
    # Enough DonSeTch results about Acme: Exa is not called.
    assert run(search("q", 4, "Acme Inc")) == {"rows": [row("Acme news", "https://n.com/1"), row("About Acme Inc", "https://acme.com"), row("Other", "https://o.com")], "provider": "donsetch", "cost": 0}
    assert len(exa_calls) == before
    # Too little about Acme: Exa tops up, results about Acme first, duplicates dropped.
    r = run(search("q", 8, "Acme"))
    assert r["provider"] == "donsetch+exa"
    assert r["cost"] == 0.007
    assert [x["url"] for x in r["rows"]] == ["https://n.com/1", "https://acme.com", "https://a.com/x", "https://o.com"]
    # Financial searches always add Exa, its results first, even when DonSeTch is enough.
    money = run(search("q", 4, "Acme Inc", True))
    assert money["provider"] == "donsetch+exa"
    assert [x["url"] for x in money["rows"]] == ["https://a.com/x", "https://n.com/1", "https://acme.com", "https://o.com"]


def test_financial_figures_need_the_exact_value_the_right_metric_and_the_right_company():
    sources = [
        {"id": "S1", "url": "https://n.example/z", "title": "Zoho results", "text": "Zoho reports Rs 12,313 Cr revenue and Rs 3,191 Cr profit in FY25. It has 18,000 employees."},
        {"id": "S2", "url": "https://n.example/o", "title": "Other Corp", "text": "Other Corp posted revenue of $500 million in FY25 after a strong year."},
        {"id": "S3", "url": "https://n.example/a", "title": "Zoho", "text": "Zoho adjusted revenue increased 48.9% to INR 24,315 Crores last year."},
    ]

    def f(metric, value, source_id, quote, period="FY2025", entity="Zoho"):
        return {"metric": metric, "value": value, "period": period, "basis": "reported", "entity": entity, "source_id": source_id, "quote": quote}
    good = check_financials([
        f("revenue", "Rs 2,313 Cr", "S1", "Zoho reports Rs 12,313 Cr revenue"),  # not the whole number
        f("revenue", "Rs 12,313 million", "S1", "Zoho reports Rs 12,313 Cr revenue"),  # wrong unit
        f("revenue", "$500 million", "S2", "posted revenue of $500 million in FY25"),  # other company
        f("revenue", "Rs 12,313 Cr", "S1", "Zoho reports Rs 12,313 Cr revenue"),
        f("profit", "18,000", "S1", "It has 18,000 employees"),  # wrong metric
        f("employees", "18,000", "S1", "It has 18,000 employees", "FY2031"),
    ], sources, "Zoho Corporation")
    assert good["revenue"]["value"] == "Rs 12,313 Cr"
    assert good["revenue"]["period"] == "FY2025"
    assert good["profit"] is None
    assert good["employees"]["period"] == "Unknown"  # year not in the source
    assert good["funding"] is None
    rival = {"id": "K", "url": "https://n.example/k", "title": "Kayako rivals", "text": "Kayako competes with Freshdesk, which has 210 employees in Chennai."}
    assert check_financials([f("employees", "210", "K", "Freshdesk, which has 210 employees", entity="Freshdesk")], [rival], "Kayako")["employees"] is None
    clash = [{"id": "A", "url": "https://a.example", "title": "", "text": "Swiggy annual revenue is Rs 8,887 Cr for FY26."}, {"id": "B", "url": "https://b.example", "title": "", "text": "Swiggy revenue was ~Rs 23,053 Cr in FY26."}]
    both = check_financials([f("revenue", "Rs 23,053 Cr", "B", "Swiggy revenue was ~Rs 23,053 Cr in FY26", "FY26", "Swiggy"), f("revenue", "Rs 8,887 Cr", "A", "Swiggy annual revenue is Rs 8,887 Cr for FY26", "FY26", "Swiggy")], clash, "Swiggy Limited")["revenue"]
    assert both["basis"] == "estimate"
    wiki = [{"id": "W", "url": "https://w.example", "title": "Swiggy", "text": "Revenue ₹ 163.33 billion[3] (US$1.7 billion) (2025). Swiggy Q2 revenue rose to Rs 7,571 crore."}]
    annual = check_financials([f("revenue", "Rs 7,571 crore", "W", "Swiggy Q2 revenue rose to Rs 7,571 crore", "Q2 FY27", "Swiggy"), f("revenue", "₹163.33 billion", "W", "Revenue ₹163.33 billion (US$1.7 billion) (2025)", "2025", "Swiggy")], wiki, "Swiggy")["revenue"]
    assert annual["value"] == "₹163.33 billion"
    assert annual["others"][0]["period"] == "Unknown"
    assert [o["value"] for o in both["others"]] == ["Rs 8,887 Cr"]
    assert check_financials([f("revenue", "INR 24,315 Crores", "S3", "adjusted revenue increased 48.9% to INR 24,315 Crores")], sources, "Zoho")["revenue"]["basis"] == "adjusted"
    assert check_financials([f("growth", "48.9%", "S3", "adjusted revenue increased 48.9% to INR 24,315 Crores")], sources, "Zoho")["growth"]["value"] == "48.9%"


def test_no_search_results_and_no_website_stops_before_any_claude_call(monkeypatch):
    def refuse(m, url, b):
        raise RuntimeError("no network expected")
    calls = fake_http(monkeypatch, refuse)
    with pytest.raises(RuntimeError, match="No search results"):
        run(identify({"product": "x", "criteria": ""}, {"name": "Acme", "website": ""}))
    assert calls == []


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_people_are_looked_up_only_for_accepted_companies_and_never_paid_for_twice(monkeypatch, tmp_path):
    run_id = "11111111-1111-1111-1111-111111111111"
    result = {"run": {"run_id": run_id}, "companies": [{"id": "c1", "domain": "acme.com", "people": [], "review": {"decision": None}, "buyer_titles": ["VP Support", "COO"], "buyer_departments": []}]}
    for name in (f"{run_id}.json", "leads.json"):
        write(tmp_path / name, result)
    monkeypatch.setenv("HUNTER_API_KEY", "h")
    monkeypatch.setenv("APOLLO_API_KEY", "a")

    def handler(method, url, body):
        if "hunter" in url:
            return httpx.Response(200, json={"data": {"emails": [{"value": "jo@acme.com", "first_name": "Jo", "last_name": "Li", "position": "VP Support", "provenance": "found", "confidence": 92, "verification": {"status": "valid"}}]}})
        if "api_search" in url:
            return httpx.Response(200, json={"people": [{"id": "3", "has_email": True, "title": "Engineer"}, {"id": "1", "has_email": True, "title": "coo"}, {"id": "2", "has_email": False, "title": "COO"}]})
        assert body == {"details": [{"id": "1"}]}  # only decision makers are unlocked: the engineer costs nothing
        return httpx.Response(200, json={"credits_consumed": 1, "matches": [{"id": "1", "name": "Al Ray", "title": "COO", "email": "al@acme.com", "email_status": "verified"}, None]})
    urls = fake_http(monkeypatch, handler)

    def ask(**extra):
        return {"run_id": run_id, "company_id": "c1", **extra}

    with pytest.raises(ValueError, match="Accept the company"):
        run(enrich_company(ask(provider="hunter"), tmp_path))
    run(review_company(ask(decision="rejected"), tmp_path))
    with pytest.raises(ValueError, match="Accept the company"):
        run(enrich_company(ask(provider="hunter"), tmp_path))
    with pytest.raises(ValueError, match="Unknown run"):
        run(review_company(ask(decision="accepted", run_id="../../secret"), tmp_path))
    assert urls == []

    run(review_company(ask(decision="accepted"), tmp_path))  # looks up the buyer titles with the first provider
    assert len(urls) == 1
    assert re.search(r"domain=acme.com&type=personal&required_field=full_name%2Cposition&limit=5&offset=0&job_titles=VP\+Support%2CCOO", urls[0])
    company = run(enrich_company(ask(provider="apollo"), tmp_path))
    assert [[p["name"], p["email"], p["status"], p["source"]] for p in company["people"]] == [["Jo Li", "jo@acme.com", "verified", "hunter"], ["Al Ray", "al@acme.com", "verified", "apollo"]]
    assert len(read(tmp_path / "leads.json")["companies"][0]["people"]) == 2
    assert run(save_lead(ask(person_id="hunter:jo@acme.com", saved=True), tmp_path))["people"][0]["saved"] is True
    with pytest.raises(ValueError, match="Unknown lead"):
        run(save_lead(ask(person_id="nobody", saved=True), tmp_path))
    with pytest.raises(ValueError, match="true or false"):
        run(save_lead(ask(person_id="hunter:jo@acme.com", saved="yes"), tmp_path))
    # No Claude key: the lead stays accepted and the reason the message is missing is kept; rejecting writes nothing.
    al = run(save_lead(ask(person_id="apollo:1", saved=True), tmp_path))
    ray = next(p for p in al["people"] if p["id"] == "apollo:1")
    assert ray["saved"] is True
    assert "ANTHROPIC_API_KEY" in ray["message_error"]
    assert next(p for p in run(save_lead(ask(person_id="apollo:1", saved=False), tmp_path))["people"] if p["id"] == "apollo:1")["saved"] is False

    monkeypatch.delenv("HUNTER_API_KEY")  # Apollo free plan: the decision is kept and the reason shown
    fake_http(monkeypatch, lambda m, url, b: httpx.Response(403, json={"error": "not included", "error_code": "API_INACCESSIBLE"}))
    result["companies"][0]["people"] = []
    write(tmp_path / f"{run_id}.json", result)
    blocked = run(review_company(ask(decision="accepted"), tmp_path))
    assert blocked["review"]["decision"] == "accepted"
    assert "free plan does not include its people API" in blocked["people_error"]


def test_a_result_is_stored_once_per_company_with_decisions_history_and_raw_responses(tmp_path):
    db = open_db(db_file(tmp_path))

    def all_(sql):
        return [dict(r) for r in db.execute(sql).fetchall()]

    def count(table):
        return all_(f"SELECT count(*) n FROM {table}")[0]["n"]

    def make(n, at, industry):
        return {
            "run": {"run_id": f"{n}" * 8 + "-1111-1111-1111-111111111111", "mode": "named_companies", "model_version": "m", "prompt_version": "p", "rerank_model": "r", "duration_s": 5},
            "campaign": {"title": "t", "generated_at": at, "icp": {"product": "x", "criteria": ""}}, "counts": {"requested": 1, "researched": 1, "failed": 0}, "failed": [],
            "companies": [{"id": f"{n}c", "input_name": "Acme", "name": "Acme", "domain": "acme.com", "website": "https://acme.com", "industry": industry, "size_band": "201-500", "location": "IN", "summary": "s", "status": "shortlisted", "fit_score": 60, "score_breakdown": {"problem_fit": 50}, "tier": "Qualified",
                           "evidence": [{"url": "https://acme.com", "quote": "q", "claim": "c", "supports": "timing", "source_type": "company_site", "status": "verified", "fetched_at": at}],
                           "buying_signals": [{"signal": "hiring", "date": "", "url": None}], "meets_min_evidence": True, "people": [], "review": {"decision": None},
                           "financials": {"revenue": {"value": "Rs 5 Cr", "period": "FY25", "basis": "reported", "entity": "Acme", "url": "https://n.example", "quote": "q", "others": []}, "profit": None}}],
        }
    first, second = make(1, "2026-01-01T00:00:00.000Z", "SaaS"), make(2, "2026-02-01T00:00:00.000Z", "Fintech")
    for r in (first, second, first, second):
        save_result(db, r)
    assert count("companies") == 1
    assert count("company_rankings") == 2
    assert count("evidence") == 2
    assert all_("SELECT industry, employee_count_min a, employee_count_max b, first_seen f FROM companies") == [{"industry": "Fintech", "a": 201, "b": 500, "f": "2026-01-01T00:00:00.000Z"}]
    assert all_("SELECT field, old_value, new_value FROM enrichment_history") == [{"field": "industry", "old_value": "SaaS", "new_value": "Fintech"}]
    assert len(all_("SELECT value FROM company_financials WHERE is_primary = 1")) == 2
    assert count("leads") == 0

    def decide(decision, decided_at):
        second["companies"][0]["review"] = {"decision": decision, "reason": None, "decided_by": "local user", "decided_at": decided_at}
        save_result(db, second)
    decide("accepted", "2026-02-02T00:00:00.000Z")
    person = {"id": "hunter:a", "name": "Jo", "title": "VP", "email": "Jo@acme.com", "status": "verified", "source": "hunter", "checked_at": "2026-02-02T00:00:00.000Z"}
    second["companies"][0]["people"] = [{**person, "saved": True}, {**person, "id": "apollo:1", "email": "jo@acme.com", "source": "apollo", "saved": True}, {**person, "id": "hunter:b", "name": "Not saved", "email": "ns@acme.com"}]
    decide("rejected", "2026-02-03T00:00:00.000Z")
    assert all_("SELECT action, previous_status p FROM review_decisions ORDER BY decided_at") == [{"action": "accepted", "p": "review_required"}, {"action": "rejected", "p": "accepted"}]
    assert all_("SELECT status FROM leads") == [{"status": "rejected"}]
    assert count("people") == 1  # same email from two providers is one person
    assert count("person_sources") == 2
    for p in second["companies"][0]["people"]:
        p["saved"] = False
    save_result(db, second)  # rejecting removes the lead
    assert count("people") + count("company_people") + count("person_sources") == 0

    async def ok():
        return {"ok": 1}

    async def fails():
        raise RuntimeError("402")
    run(with_job(tmp_path, "job-without-result", lambda: recorded("exa", "search", {"query": "q"}, ok, lambda raw: {"cost": 0.007})))
    with pytest.raises(RuntimeError, match="402"):
        run(with_job(tmp_path, "job-without-result", lambda: recorded("exa", "search", {}, fails)))
    assert all_("SELECT status, cost_estimate c, error_code e FROM provider_runs") == [{"status": "ok", "c": 0.007, "e": None}, {"status": "error", "c": None, "e": "402"}]
    assert count("provider_responses") == 1

    broken = make(3, "2026-03-01T00:00:00.000Z", "x")
    broken["companies"][0]["evidence"][0]["quote"] = None
    with pytest.raises(Exception):
        save_result(db, broken)
    assert all_("SELECT id FROM search_jobs WHERE id LIKE '33333333%'") == []  # rolled back

    write(tmp_path / f"{second['run']['run_id']}.json", second)
    assert sync_db(tmp_path) == 1
    assert count("companies") == 1


def test_hunter_tries_buyer_titles_then_senior_people_in_the_buyer_department_then_a_few_decision_makers(monkeypatch, tmp_path):
    run_id = "22222222-2222-2222-2222-222222222222"
    write(tmp_path / f"{run_id}.json", {"run": {"run_id": run_id}, "companies": [{"id": "c1", "domain": "acme.com", "people": [], "review": {"decision": None}, "buyer_titles": ["Head of Support"], "buyer_departments": ["support"],
                                                                         "financials": {"employees": {"value": "5,000"}}}]})
    monkeypatch.setenv("HUNTER_API_KEY", "h")
    urls = fake_http(monkeypatch, lambda m, url, b: httpx.Response(200, json={"data": {"emails": [] if re.search(r"job_titles|department", url) else [{"value": "ceo@acme.com", "first_name": "Sam", "last_name": "Ko", "position": "CEO", "verification": {"status": "valid"}}]}}))
    company = run(review_company({"run_id": run_id, "company_id": "c1", "decision": "accepted"}, tmp_path))
    assert len(urls) == 3
    assert re.search(r"job_titles=Head\+of\+Support&seniority=executive", urls[0])
    assert re.search(r"limit=5&offset=0&department=support&seniority=executive$", urls[1])
    assert re.search(r"limit=3&offset=0&seniority=executive&decision_maker=true", urls[2])
    assert [[p["name"], p["reason"]] for p in company["people"]] == [["Sam Ko", "Decision maker (no one found with the buyer titles)"]]
    # a small company skips the department search: it would mostly return directors, which aren't kept, and still cost a credit
    write(tmp_path / f"{run_id}.json", {"run": {"run_id": run_id}, "companies": [{"id": "c1", "domain": "acme.com", "people": [], "review": {"decision": None}, "buyer_titles": ["CEO"], "buyer_departments": ["support"],
                                                                         "financials": {"employees": {"value": "51-200"}}}]})
    urls.clear()
    run(review_company({"run_id": run_id, "company_id": "c1", "decision": "accepted"}, tmp_path))
    assert len(urls) == 2 and not any("department=" in u for u in urls)


def test_finding_more_leads_pages_through_hunter_and_never_returns_anyone_seen_before(monkeypatch, tmp_path):
    run_id, old_run = "33333333-3333-3333-3333-333333333333", "44444444-4444-4444-4444-444444444444"
    company = {"id": "c1", "domain": "acme.com", "people": [], "review": {"decision": None}, "buyer_titles": ["VP Support"], "buyer_departments": []}
    write(tmp_path / f"{run_id}.json", {"run": {"run_id": run_id}, "companies": [company]})
    write(tmp_path / f"{old_run}.json", {"run": {"run_id": old_run}, "companies": [{**company, "id": "old", "people": [{"id": "hunter:p0@acme.com", "name": "Old Lead", "email": "p0@acme.com", "saved": False}]}]})
    monkeypatch.setenv("HUNTER_API_KEY", "h")
    everyone = [{"value": f"p{i}@acme.com", "first_name": "P", "last_name": str(i), "position": "VP Support"} for i in range(7)]

    def handler(method, url, body):
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        start = int(q["offset"])
        return httpx.Response(200, json={"data": {"emails": everyone[start:start + int(q["limit"])]}})
    urls = fake_http(monkeypatch, handler)
    first = run(review_company({"run_id": run_id, "company_id": "c1", "decision": "accepted"}, tmp_path))

    def newest(c):
        top = max(p["batch"] for p in c["people"])
        return [p["email"] for p in c["people"] if p["batch"] == top]
    assert next(p for p in first["people"] if p.get("from_run"))["saved"] is False  # earlier run's lead comes back with its decision
    assert newest(first) == ["p1@acme.com", "p2@acme.com", "p3@acme.com", "p4@acme.com", "p5@acme.com"]  # p0 skipped, the next page fills the 5th
    more = run(enrich_company({"run_id": run_id, "company_id": "c1", "provider": "hunter"}, tmp_path))
    assert newest(more) == ["p6@acme.com"]
    none = run(enrich_company({"run_id": run_id, "company_id": "c1", "provider": "hunter"}, tmp_path))
    assert none["people_lookups"][-1]["found"] == 0
    assert len(urls) == 3  # no search once Hunter has no one left


def test_figures_under_the_parents_name_count_and_growth_can_be_calculated():
    src = [{"id": "S1", "url": "https://n.example/e", "title": "Eternal results", "text": "Eternal reported revenue of ₹54,364 crore in FY26 (the year to March 2026), against ₹20,243 crore in FY25, with a net profit of ₹366 crore."}]

    def item(metric, value, quote, entity="Eternal Limited (Zomato parent) consolidated"):
        return {"metric": metric, "value": value, "period": "FY2026", "basis": "reported", "entity": entity, "source_id": "S1", "quote": quote}
    quote = "Eternal reported revenue of ₹54,364 crore in FY26 (the year to March 2026), against ₹20,243 crore in FY25"
    f = check_financials([item("revenue", "₹54,364 crore", quote), item("profit", "₹366 crore", "with a net profit of ₹366 crore"), item("growth", "+169%", quote)], src, "Zomato (Eternal Limited)")
    assert f["revenue"]["value"] == "₹54,364 crore"
    assert f["profit"]["value"] == "₹366 crore"
    assert [f["growth"]["value"], f["growth"]["basis"]] == ["+168.6%", "calculated"]
    # A growth figure that doesn't match the sum, or a company with no such parent name, is still rejected.
    assert check_financials([item("growth", "+120%", quote)], src, "Zomato (Eternal Limited)")["growth"] is None
    assert check_financials([item("revenue", "₹54,364 crore", quote)], src, "Zomato")["revenue"] is None


def test_messages_are_signed_kept_under_linkedin_limit_rewritten_and_tracked(monkeypatch, tmp_path):
    from agent import people
    run_id = "55555555-5555-5555-5555-555555555555"
    lead = {"id": "hunter:jo@acme.com", "name": "Jo Li", "title": "VP Support", "email": "jo@acme.com"}
    write(tmp_path / f"{run_id}.json", {"run": {"run_id": run_id}, "campaign": {"icp": {"product": "voice agent"}}, "companies": [{"id": "c1", "name": "Acme", "domain": "acme.com", "people": [dict(lead)], "review": {"decision": "accepted"},
        "growth_play": "Acme could answer calls 24/7 like Beta.", "growth_levers": ["Answer calls at night -> more orders"], "peers": [{"name": "Beta", "what_they_did": "Automated support calls.", "growth": "wait times halved", "quote": "Beta cut wait times by half"}]}]})
    monkeypatch.setenv("SENDER_NAME", "Asha Rao")
    calls = []

    async def fake_ask(system, prompt, schema, effort, max_tokens):
        calls.append((system, prompt))
        note = "x" * 320 if len(calls) == 1 else "short note"
        return {"data": {"subject": "s", "email": "e", "linkedin_note": note, "followup": "f"}, "usage": {"usd": 0.002}}
    monkeypatch.setattr(people, "ask_json", fake_ask)

    def ask(**extra):
        return {"run_id": run_id, "company_id": "c1", "person_id": lead["id"], **extra}

    with pytest.raises(ValueError, match="Accept the lead first"):
        run(people.rewrite_message(ask(feedback="shorter"), tmp_path))
    m = run(people.save_lead(ask(saved=True), tmp_path))["people"][0]["message"]
    assert len(calls) == 2 and "320 characters" in calls[1][1]  # too long: rewritten once, not cut
    assert m["linkedin_note"] == "short note" and m["followup"] == "f" and m["usd"] == 0.004
    assert 'signed "Asha Rao"' in calls[0][0]
    assert "How the product helps them grow: Acme could answer calls 24/7 like Beta." in calls[0][1] and 'Beta: Automated support calls. Result: wait times halved (quote: "Beta cut wait times by half")' in calls[0][1] and "Way to grow: Answer calls at night -> more orders" in calls[0][1]
    run(people.rewrite_message(ask(feedback="mention their hiring"), tmp_path))
    assert "mention their hiring" in calls[-1][1] and '"linkedin_note":"short note"' in calls[-1][1]  # sees the current draft
    with pytest.raises(ValueError, match="Say what to change"):
        run(people.rewrite_message(ask(feedback=" "), tmp_path))
    assert run(people.mark_outreach(ask(status="sent"), tmp_path))["people"][0]["outreach"]["status"] == "sent"
    assert "outreach" not in run(people.mark_outreach(ask(status=None), tmp_path))["people"][0]
    with pytest.raises(ValueError, match="Status must be"):
        run(people.mark_outreach(ask(status="maybe"), tmp_path))


def test_the_product_can_come_from_a_deck_or_website(monkeypatch):
    import base64
    import io
    import zipfile
    from agent import pipeline
    deck = io.BytesIO()
    with zipfile.ZipFile(deck, "w") as z:
        z.writestr("ppt/slides/slide10.xml", "<p:sld><a:t>Trusted by Acme</a:t></p:sld>")
        z.writestr("ppt/slides/slide2.xml", "<p:sld><a:t>VoiceBot answers</a:t><a:t>support calls &amp; cuts cost</a:t></p:sld>")
        z.writestr("ppt/slides/slide11.xml", "<p:sld><a:t>" + "Banks and retailers use VoiceBot to answer every call in seconds. " * 3 + "</a:t></p:sld>")
    b64 = base64.b64encode(deck.getvalue()).decode()
    assert pipeline.file_text("pitch.pptx", b64).startswith("VoiceBot answers support calls & cuts cost\nTrusted by Acme\nBanks and retailers")
    with pytest.raises(ValueError, match="PDF, PPTX"):
        pipeline.file_text("virus.exe", b64)

    seen = {}

    async def fake_fetch(url, max_chars=6000):
        return {"url": url, "title": "VoiceBot", "description": "", "text": "AI phone agents for support teams. " * 10, "links": [url + "/about", url + "/careers"]}

    async def fake_ask(system, prompt, schema, **_):
        seen["prompt"] = prompt
        return {"data": {"usable": True, "problem": "", "product": " VoiceBot: AI voice agent for support. ", "category": "AI voice agent "}, "usage": {"input_tokens": 1, "output_tokens": 1, "usd": 0.001}}
    monkeypatch.setattr(pipeline, "fetch_page", fake_fetch)
    monkeypatch.setattr(pipeline, "ask_json", fake_ask)
    got = run(pipeline.describe_product("focus on India", "voicebot.ai", {"name": "pitch.pptx", "data": b64}))
    assert got == {"product": "VoiceBot: AI voice agent for support.", "category": "AI voice agent", "usd": 0.001}
    # the website, its about page (not careers), the deck and the user's notes all reach Claude
    assert "Page https://voicebot.ai/about" in seen["prompt"] and "Page https://voicebot.ai/careers" not in seen["prompt"]
    assert "cuts cost" in seen["prompt"] and "focus on India" in seen["prompt"]


def test_the_users_notes_rejudge_a_company_and_count_as_evidence(monkeypatch, tmp_path):
    from agent import people
    run_id = "66666666-6666-6666-6666-666666666666"
    company = {"id": "c1", "name": "Acme", "domain": "acme.com", "fit_score": 20, "tier": "Disqualify", "identity_confidence": "high", "people": [],
               "score_breakdown": {"problem_fit": 30, "profile_fit": 20, "timing": 10, "role_clarity": 10, "evidence_quality": 50},
               "evidence": [{"status": "verified", "supports": "problem_fit", "claim": "c", "quote": "q"}], "review": {"decision": None}}
    write(tmp_path / f"{run_id}.json", {"run": {"run_id": run_id}, "campaign": {"icp": {"product": "voice agent"}}, "companies": [company]})
    seen = {}

    async def fake_ask(system, prompt, schema, **_):
        seen["prompt"] = prompt
        return {"data": {"verdict": "Better now.", "willingness_to_buy": "medium", "pitch_angle": "p", "scores": {"problem_fit": 80, "profile_fit": 70, "timing": 90, "role_clarity": 60},
                         "notes_support": ["timing"], "fit_reasons": [], "objections": [], "next_steps": [], "growth_business": ["b -> c"], "growth_technical": ["t -> u"], "growth_play": "g", "product_solutions": [{"growth_point": "b", "how_product_helps": "h"}], "product_extras": [], "path_to_win": ["w"]},
                "usage": {"usd": 0.02}}
    monkeypatch.setattr(people, "ask_json", fake_ask)
    with pytest.raises(ValueError, match="Write what you know"):
        run(people.apply_notes({"run_id": run_id, "company_id": "c1", "notes": "  "}, tmp_path))
    got = run(people.apply_notes({"run_id": run_id, "company_id": "c1", "notes": "Budget approved in March."}, tmp_path))
    assert "Budget approved in March." in seen["prompt"]
    # problem_fit has a verified quote and timing is backed by the notes; the other two stay capped at 35
    assert got["score_breakdown"] == {"problem_fit": 80, "profile_fit": 35, "timing": 90, "role_clarity": 35, "evidence_quality": 50}
    assert got["fit_score"] == 62 and got["tier"] == "Qualified" and got["willingness_to_buy"] == "medium"
    assert got["user_notes"]["score_before"] == 20 and got["growth_technical"] == ["t -> u"] and got["product_solutions"][0]["how_product_helps"] == "h"
    # a second update still remembers the score before any notes
    assert run(people.apply_notes({"run_id": run_id, "company_id": "c1", "notes": "More."}, tmp_path))["user_notes"]["score_before"] == 20


def test_the_case_study_needs_proof_of_the_same_work_and_of_its_growth_figures():
    from agent.pipeline import check_case_study
    news = ("Anand Rathi Wealth is a wealth management firm serving HNI families in India. Anand Rathi Wealth grew revenue from Rs 420 crore in FY2023 to Rs 750 crore in FY2025 with a digital advisory platform. "
            "Waterfield Advisors is a multi-family office serving wealthy families. TVS Wealth manages money for HNI families as a wealth management firm, revenue Rs 90 crore.")
    sources = [{"id": "S1", "url": "https://news.example/wealth", "title": "Wealth firms", "type": "peer", "text": news},
               {"id": "S2", "url": "https://growjo.com/company/International_Money_Matters", "title": "IMMPL competitors", "type": "third_party",
                "text": "Sayari Labs is a financial intelligence company with revenue of $64.8M and 58% employee growth."}]
    case = lambda name, work, quote, growth, sid="S1": {"case_study": {"name": name, "what_they_do": "wealth management", "work_source_id": sid, "work_quote": work,
                                                                   "what_they_did": "Digital advisory platform", "growth_metric": "revenue", "growth": growth,
                                                                   "source_id": sid, "quote": quote, "how_product_helps": ["they automated advice -> IMMPL can serve 400 clients"]},
                                                    "related_companies": ["TVS Capital Funds Private Limited"],
                                                    "similar_companies": [{"name": "Waterfield Advisors", "work_source_id": "S1", "work_quote": "Waterfield Advisors is a multi-family office serving wealthy families"},
                                                                          {"name": "BNY Pershing", "work_source_id": "S1", "work_quote": "a wealth management firm serving HNI families in India"}]}
    good = case("Anand Rathi Wealth", "Anand Rathi Wealth is a wealth management firm serving HNI families", "Anand Rathi Wealth grew revenue from Rs 420 crore in FY2023 to Rs 750 crore in FY2025",
                "revenue Rs 420 crore (FY2023) to Rs 750 crore (FY2025)")
    got = check_case_study(good, sources, "International Money Matters Private Limited")
    assert got["case_study"]["name"] == "Anand Rathi Wealth" and got["case_study"]["url"] == "https://news.example/wealth"
    # BNY Pershing's quote doesn't name it, so it isn't proof
    assert got["similar_companies"] == ["Anand Rathi Wealth", "Waterfield Advisors"]
    imm = "International Money Matters Private Limited"
    # an invented figure, a sister company, an auto-generated competitors page: no case study, rather than a wrong one
    assert check_case_study({**good, "case_study": {**good["case_study"], "growth": "revenue Rs 420 crore to Rs 900 crore"}}, sources, imm)["case_study"] is None
    assert check_case_study(case("TVS Wealth", "TVS Wealth manages money for HNI families as a wealth management firm", "TVS Wealth manages money for HNI families as a wealth management firm, revenue Rs 90 crore", "revenue Rs 90 crore"), sources, imm)["case_study"] is None
    assert check_case_study(case("Sayari Labs", "Sayari Labs is a financial intelligence company", "Sayari Labs is a financial intelligence company with revenue of $64.8M", "$64.8M", "S2"), sources, imm)["case_study"] is None
    # nothing proven -> nothing shown, and the company's own brands never count
    assert check_case_study({"similar_companies": [{"name": "Zoho Books", "work_source_id": "S1", "work_quote": "Zoho Books is accounting software for small businesses"}]},
                            [{"id": "S1", "url": "https://x.example", "title": "", "text": "Zoho Books is accounting software for small businesses."}], "Zoho Corporation") == {"case_study": None, "similar_companies": []}


def test_the_most_recent_revenue_figure_comes_first():
    from agent.pipeline import check_financials
    text = "Acme revenue was Rs 100 crore in FY2023. Acme revenue rose to Rs 180 crore in FY2025."
    sources = [{"id": "S1", "url": "https://news.example/acme", "title": "Acme results", "text": text}]
    item = lambda v, period, quote: {"metric": "revenue", "value": v, "period": period, "basis": "reported", "entity": "Acme", "source_id": "S1", "quote": quote}
    got = check_financials([item("Rs 100 crore", "FY2023", "Acme revenue was Rs 100 crore in FY2023"), item("Rs 180 crore", "FY2025", "Acme revenue rose to Rs 180 crore in FY2025")], sources, "Acme")
    assert got["revenue"]["value"] == "Rs 180 crore" and [o["value"] for o in got["revenue"]["others"]] == ["Rs 100 crore"]
def test_wrong_or_broken_product_files_are_refused_with_a_clear_reason(monkeypatch):
    import base64
    import io
    import zipfile
    from agent import pipeline
    b64 = lambda raw: base64.b64encode(raw).decode()
    for name, raw, why in [("deck.pdf", b"PK not a pdf", "isn't a real PDF"), ("deck.pdf", b"%PDF-1.4 broken", "damaged"),
                           ("deck.pptx", b"%PDF-1.4", "isn't a real PPTX"), ("notes.txt", b"\0\1\2", "isn't plain text")]:
        with pytest.raises(ValueError, match=why):
            pipeline.file_text(name, b64(raw))
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("word/document.xml", "<w:t>a resume</w:t>")
    with pytest.raises(ValueError, match="no slides"):
        pipeline.file_text("cv.pptx", b64(empty.getvalue()))

    calls = []

    async def fake_ask(system, prompt, schema, **_):
        calls.append(prompt)
        return {"data": {"usable": False, "problem": "This is a personal resume", "product": "", "category": ""}, "usage": {"usd": 0.001}}
    monkeypatch.setattr(pipeline, "ask_json", fake_ask)
    with pytest.raises(ValueError, match="almost no readable text"):  # too little text: refused before any Claude call
        run(pipeline.describe_product("", "", {"name": "x.txt", "data": b64(b"Hi")}))
    assert not calls
    with pytest.raises(ValueError, match="file isn't about your company or what it sells: This is a personal resume. Use your own"):
        run(pipeline.describe_product("", "", {"name": "cv.txt", "data": b64(b"Jane Doe. Experience: 5 years as an accountant. " * 5)}))



def test_a_schema_the_api_refuses_as_too_large_falls_back_and_never_fails_the_company(monkeypatch):
    import anthropic
    from agent import llm
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    sent = []

    class Stream:
        def __init__(self, kw):
            self.kw = kw

        async def __aenter__(self):
            sent.append(self.kw)
            if "format" in self.kw["output_config"]:
                raise anthropic.BadRequestError("The compiled grammar is too large", response=httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com")), body=None)
            return self

        async def __aexit__(self, *exc):
            return False

        async def get_final_message(self):
            class Msg:
                def model_dump(self, mode):
                    text = 'Here you go: {"verdict": "ok", "scores": {"problem_fit": 70}, "evidence": [{"quote": "q"}]}'
                    return {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}], "usage": {"input_tokens": 10, "output_tokens": 5}}
            return Msg()

    class Client:
        class messages:
            @staticmethod
            def stream(**kw):
                return Stream(kw)
    monkeypatch.setattr(llm, "_client", Client())
    schema = {"type": "object", "properties": {"verdict": {"type": "string"}, "willingness": {"type": "string", "enum": ["high", "medium", "low"]},
                                               "scores": {"type": "object", "properties": {"problem_fit": {"type": "integer"}, "timing": {"type": "integer"}}},
                                               "evidence": {"type": "array", "items": {"type": "object", "properties": {"quote": {"type": "string"}, "claim": {"type": "string"}}}}}}
    got = run(llm.ask_json("system", "prompt", schema))
    assert len(sent) == 2 and "format" not in sent[1]["output_config"] and "JSON schema" in sent[1]["system"][0]["text"] and sent[1]["system"][0]["cache_control"] == {"type": "ephemeral"}
    # missing fields are filled so the code reading the reply never breaks
    assert got["data"] == {"verdict": "ok", "willingness": "low", "scores": {"problem_fit": 70, "timing": 0}, "evidence": [{"quote": "q", "claim": ""}]}
    # the refusal is remembered: the next call with this schema skips the refused request
    run(llm.ask_json("system", "prompt", schema))
    assert len(sent) == 3 and "format" not in sent[2]["output_config"]


def test_figures_from_an_annual_report_table_count_and_growth_comes_from_the_two_year_row():
    from agent.pipeline import check_financials
    text = ("International Money Matters Private Limited. Statement of Profit and Loss for the year ended 31 March 2024 (Rs in lakhs). "
            "Revenue from operations 19 1,300.00 1,112.88. Profit after tax 103.75 16.83. FY2024")
    sources = [{"id": "S20", "url": "https://x.example/ar", "title": "IMMPL annual report FY2024", "text": text},
               {"id": "S19", "url": "https://estimates.example", "title": "IMMPL", "text": "International Money Matters Pvt Ltd has an annual revenue of $352.9K"}]
    item = lambda metric, value, period, sid, quote: {"metric": metric, "value": value, "period": period, "basis": "reported", "entity": "International Money Matters Private Limited", "source_id": sid, "quote": quote}
    got = check_financials([item("revenue", "₹1,300.00 lakhs", "FY2024", "S20", "Revenue from operations 19 1,300.00 1,112.88"),
                            item("profit", "₹103.75 lakhs", "FY2024", "S20", "Profit after tax 103.75 16.83"),
                            item("revenue", "$352.9K", "Unknown", "S19", "has an annual revenue of $352.9K"),
                            # a unit the page never states is still refused
                            item("funding", "₹50 crore", "FY2024", "S20", "Profit after tax 103.75 16.83")], sources, "International Money Matters Pvt Ltd")
    assert (got["revenue"]["value"], got["revenue"]["period"]) == ("₹1,300.00 lakhs", "FY2024") and [o["value"] for o in got["revenue"]["others"]] == ["$352.9K"]
    assert got["profit"]["value"] == "₹103.75 lakhs"
    assert (got["growth"]["value"], got["growth"]["basis"]) == ("+16.8%", "calculated")
    assert got["funding"] is None


def test_the_case_study_runs_on_demand_and_keeps_only_proven_companies(monkeypatch, tmp_path):
    from agent import people, pipeline
    run_id = "77777777-7777-7777-7777-777777777777"
    company = {"id": "c1", "name": "IMMPL", "domain": "immpl.com", "business_type": "wealth management for HNI families", "region": "India",
               "related_companies": ["TVS Capital"], "people": [], "review": {"decision": None}}
    write(tmp_path / f"{run_id}.json", {"run": {"run_id": run_id}, "campaign": {"icp": {"product": "Flow AI platform"}}, "companies": [company]})
    page = "Nuvama Wealth is a wealth management firm serving HNI and UHNI clients in India. Nuvama Wealth revenue rose to Rs 2,901 crore in FY25, up 24%."
    queries = []

    async def fake_search(query, max_results=8, about="", also_exa=False):
        queries.append((query, also_exa))
        return {"rows": [{"url": "https://rank.example/top-wealth", "title": "Top wealth firms", "snippet": page},
                         {"url": "https://growjo.com/x", "title": "IMMPL competitors", "snippet": "Sayari Labs revenue $64.8M"}], "provider": "donsetch+exa", "cost": 0.007 if also_exa else 0}

    async def fake_fetch(url, max_chars=6000):
        return None

    async def fake_ask(system, prompt, schema, **_):
        assert "growjo" not in prompt and "TVS Capital" in system  # look-alike pages never reach Claude; owners are named as excluded
        return {"data": {"case_study": {"name": "Nuvama Wealth", "what_they_do": "wealth management", "work_source_id": "S1",
                                        "work_quote": "Nuvama Wealth is a wealth management firm serving HNI and UHNI clients", "what_they_did": "Grew its adviser platform.",
                                        "growth_metric": "revenue", "growth": "revenue Rs 2,901 crore (FY25), up 24%", "source_id": "S1",
                                        "quote": "Nuvama Wealth revenue rose to Rs 2,901 crore in FY25, up 24%", "implemented": ["Opened UHNI offices in 3 cities", "Moved client reporting to a digital platform"], "how_product_helps": ["they scaled advisers -> IMMPL can automate reports"]},
                         "similar_companies": []}, "usage": {"usd": 0.02}}
    monkeypatch.setattr(pipeline, "search", fake_search)
    monkeypatch.setattr(pipeline, "fetch_page", fake_fetch)
    monkeypatch.setattr(pipeline, "ask_json", fake_ask)
    got = run(people.case_study({"run_id": run_id, "company_id": "c1"}, tmp_path))
    assert got["case_study"]["name"] == "Nuvama Wealth" and got["similar_companies"] == ["Nuvama Wealth"]
    assert got["case_study"]["implemented"] == ["Opened UHNI offices in 3 cities", "Moved client reporting to a digital platform"]
    assert got["case_study_search"]["found"] and got["case_study_search"]["usd"] == 0.034
    assert any(exa for _, exa in queries) and "wealth management for HNI families" in queries[0][0]


def test_a_case_study_quote_may_join_bullet_points_if_every_part_is_on_the_page():
    from agent.pipeline import check_case_study
    text = ("How Spire Streamlines Wealth Management Across 15K Accounts with StratiFi. Results: 15K accounts and $5B in assets managed through StratiFi. "
            "45 minutes saved per proposal. 30% revenue growth across one wealth management group.")
    sources = [{"id": "S1", "url": "https://stratifi.example/spire", "title": "Spire case study", "text": text}]
    case = {"name": "Spire", "what_they_do": "wealth management", "work_source_id": "S1", "work_quote": "How Spire Streamlines Wealth Management Across 15K Accounts with StratiFi",
            "what_they_did": "AI platform", "growth_metric": "revenue", "growth": "30% revenue growth", "source_id": "S1",
            "quote": "15K accounts and $5B in assets managed through StratiFi - 30% revenue growth across one wealth management group", "how_product_helps": []}
    assert check_case_study({"case_study": case}, sources, "IMMPL")["case_study"]["name"] == "Spire"
    changed = {**case, "quote": "15K accounts and $5B in assets managed through StratiFi - 90% revenue growth across one wealth management group"}
    assert check_case_study({"case_study": changed}, sources, "IMMPL")["case_study"] is None


def test_a_case_study_quote_may_sit_in_the_next_passage_of_the_same_page():
    from agent.pipeline import check_case_study
    url = "https://stratifi.example/spire"
    sources = [{"id": "S12", "url": url, "title": "Spire case study", "text": "How Spire Streamlines Wealth Management Across 15K Accounts and $5B in Assets with StratiFi."},
               {"id": "S13", "url": url, "title": "Spire case study", "text": "Results: 30% revenue growth across one wealth management group."},
               {"id": "S14", "url": "https://other.example", "title": "Other", "text": "Acme grew 90% revenue growth across one wealth management group."}]
    case = {"name": "Spire", "what_they_do": "wealth management", "work_source_id": "S12", "work_quote": "How Spire Streamlines Wealth Management Across 15K Accounts and $5B in Assets",
            "what_they_did": "AI platform", "growth_metric": "revenue", "growth": "30% revenue growth across 15K accounts and $5B AUM", "source_id": "S12",
            "quote": "30% revenue growth across one wealth management group", "how_product_helps": []}
    assert check_case_study({"case_study": case}, sources, "IMMPL")["case_study"]["name"] == "Spire"
    # but a figure from a different page still doesn't count
    assert check_case_study({"case_study": {**case, "growth": "90% revenue growth"}}, sources, "IMMPL")["case_study"] is None


def test_excel_export_is_a_real_workbook_and_web_text_never_becomes_a_formula():
    import io
    from openpyxl import load_workbook
    from agent.export import xlsx
    data = xlsx([{"name": "Companies", "rows": [["name", "fit_score", "verdict"], ["Acme", 62, "=HYPERLINK(\"http://evil\")"], ["Beta", None, "ok"]]}])
    ws = load_workbook(io.BytesIO(data))["Companies"]
    assert [c.value for c in ws[1]] == ["name", "fit_score", "verdict"] and ws["A1"].font.bold
    assert ws["B2"].value == 62 and ws["C2"].data_type == "s" and ws["C2"].value.startswith("=HYPERLINK") and ws["B3"].value in ("", None)
    for bad in (None, [], [{"name": "x", "rows": "nope"}]):
        with pytest.raises(ValueError):
            xlsx(bad)


def test_only_decision_makers_are_kept_and_directors_only_at_large_companies(monkeypatch, tmp_path):
    from agent.people import senior_enough
    for title, small, large in [("CEO", True, True), ("Co-Founder & CTO", True, True), ("VP Engineering", True, True), ("Managing Director", True, True),
                                ("Director of Operations", False, True), ("Head of Support", False, True), ("Product Manager", False, False),
                                ("Executive Assistant to the CEO", False, False), ("Chief of Staff", False, False), ("Associate Director", False, False)]:
        assert (senior_enough(title, False), senior_enough(title, True)) == (small, large), title
    run_id = "88888888-8888-8888-8888-888888888888"
    people = [("ceo@acme.com", "Sam", "CEO"), ("dir@acme.com", "Dee", "Director of Operations"), ("pm@acme.com", "Pat", "Product Manager")]
    monkeypatch.setenv("HUNTER_API_KEY", "h")
    fake_http(monkeypatch, lambda m, url, b: httpx.Response(200, json={"data": {"emails": [{"value": e, "first_name": n, "last_name": "X", "position": t, "verification": {"status": "valid"}} for e, n, t in people] if "job_titles" in url else []}}))
    for employees, expected in [("51-200", ["Sam X"]), ("8,053", ["Sam X", "Dee X"])]:
        write(tmp_path / f"{run_id}.json", {"run": {"run_id": run_id}, "companies": [{"id": "c1", "domain": "acme.com", "people": [], "review": {"decision": None}, "buyer_titles": ["CEO"], "buyer_departments": [],
                                                                                 "financials": {"employees": {"value": employees}}}]})
        company = run(review_company({"run_id": run_id, "company_id": "c1", "decision": "accepted"}, tmp_path))
        assert [p["name"] for p in company["people"]] == expected, employees


def test_cached_instructions_are_billed_at_their_real_price(monkeypatch):
    from agent import llm
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-5")  # $2 in / $10 out per million
    plain = llm._usage({"input_tokens": 10_000, "output_tokens": 1_000})
    cached = llm._usage({"input_tokens": 7_500, "cache_read_input_tokens": 2_500, "output_tokens": 1_000})
    written = llm._usage({"input_tokens": 7_500, "cache_creation_input_tokens": 2_500, "output_tokens": 1_000})
    assert plain["usd"] == 0.03 and round(cached["usd"], 4) == 0.0255 and round(written["usd"], 5) == 0.03125
    assert cached["input_tokens"] == 10_000



def test_recent_research_is_reused_only_for_the_same_product_and_version(tmp_path):
    from agent.pipeline import PROMPT_VERSION, recent_research
    run_id = "99999999-9999-9999-9999-999999999999"
    old = {"run": {"run_id": run_id, "prompt_version": PROMPT_VERSION}, "campaign": {"generated_at": "2026-11-24T08:00:00.000Z", "icp": {"product": "Flow", "criteria": ""}},
           "companies": [{"id": "c1", "input_name": "Zoho", "name": "Zoho Corporation", "domain": "zoho.com", "fit_score": 33, "growth_play": "g",
                          "people": [{"id": "hunter:a@zoho.com", "saved": True}], "review": {"decision": "accepted"}, "paging": {"x": 1}}]}
    write(tmp_path / f"{run_id}.json", old)
    inp = {"product": "Flow", "criteria": ""}
    got = recent_research({"name": "zoho", "website": ""}, inp, tmp_path)
    assert got["fit_score"] == 33 and got["growth_play"] == "g" and got["id"] != "c1" and got["reused_from"]["run_id"] == run_id
    assert got["people"] == [] and got["review"]["decision"] is None and "paging" not in got and got["usage"]["usd"] == 0
    assert recent_research({"name": "Zoho Books", "website": "zoho.com"}, inp, tmp_path)["name"] == "Zoho Corporation"  # matched by domain
    assert recent_research({"name": "zoho", "website": ""}, {"product": "Other product", "criteria": ""}, tmp_path) is None
    assert recent_research({"name": "Swiggy", "website": ""}, inp, tmp_path) is None
    write(tmp_path / f"{run_id}.json", {**old, "run": {**old["run"], "prompt_version": "named-v1"}})
    assert recent_research({"name": "zoho", "website": ""}, inp, tmp_path) is None


def test_the_growth_plan_is_written_on_demand_once_and_again_only_when_asked(monkeypatch, tmp_path):
    from agent import people
    run_id = "12121212-1212-1212-1212-121212121212"
    write(tmp_path / f"{run_id}.json", {"run": {"run_id": run_id}, "campaign": {"icp": {"product": "Flow"}},
                                        "companies": [{"id": "c1", "name": "Acme", "summary": "A wealth firm", "people": [], "review": {"decision": None}, "evidence": [], "financials": {}}]})
    calls = []

    async def fake_ask(system, prompt, schema, **_):
        calls.append(prompt)
        return {"data": {"growth_business": ["b -> c"], "growth_technical": ["t -> u"], "growth_play": "g", "product_solutions": [], "product_extras": [], "path_to_win": ["w"]},
                "usage": {"usd": 0.02}}
    monkeypatch.setattr(people, "ask_json", fake_ask)
    ask = {"run_id": run_id, "company_id": "c1"}
    got = run(people.growth_plan(ask, tmp_path))
    assert got["growth_business"] == ["b -> c"] and got["growth_written"]["usd"] == 0.02 and "A wealth firm" in calls[0]
    run(people.growth_plan(ask, tmp_path))  # already written: free
    assert len(calls) == 1
    run(people.growth_plan({**ask, "again": True}, tmp_path))
    assert len(calls) == 2


def test_buying_capacity_is_a_shown_rule_of_thumb_from_verified_revenue():
    from agent.capacity import buying_capacity, parse_money
    assert parse_money("₹17.66 Cr") == (176600000.0, "INR") and parse_money("$7.6 million") == (7600000.0, "USD")
    assert parse_money("₹4.62 lakh crore") == (4.62e12, "INR") and parse_money("51-200") is None
    got = buying_capacity({"revenue": {"value": "₹17.66 Cr", "period": "FY2025"}, "growth": {"value": "31%"}, "profit": {"value": "₹2.23 Cr"}}, "low")
    assert got["tech_budget"] == "₹53 lakh – ₹1.2 Cr a year" and got["new_offering_budget"] == "₹5.3 lakh – ₹24.7 lakh a year"
    assert got["affinity"] == "medium" and "FY2025" in got["basis"]
    assert buying_capacity({"growth": {"value": "-12%"}, "profit": {"value": "loss of Rs 40 crore"}}, "high")["affinity"] == "low"
    assert buying_capacity({}, None) == {"affinity": "low", "reasons": ["Willingness to buy: unknown"], "tech_budget": None, "new_offering_budget": None, "basis": None}


def test_finding_companies_from_an_icp_keeps_only_named_ones_and_never_repeats(monkeypatch):
    from agent import discover
    monkeypatch.setenv("EXA_API_KEY", "x")
    seen = {}

    async def fake_search(query, max_results=8, about="", also_exa=False):
        return {"rows": [{"url": "https://lists.example/top-saas-bengaluru", "title": "Top SaaS companies in Bengaluru", "snippet": "Freshworks, Zoho, Chargebee and Darwinbox are leading SaaS firms in Bengaluru."}], "provider": "donsetch", "cost": 0}

    async def fake_exa(query, max_results, category=None):
        seen["category"] = category
        return {"rows": [{"url": "https://www.chargebee.com/", "title": "Chargebee - subscription billing", "snippet": "Chargebee is a Bengaluru SaaS company."}], "cost": 0.007}

    async def fake_fetch(url, max_chars=6000):
        return None

    async def fake_ask(system, prompt, schema, **_):
        seen["prompt"] = prompt
        pick = lambda name, domain, sid: {"name": name, "domain": domain, "why": "SaaS in Bengaluru", "source_id": sid}
        return {"data": {"companies": [pick("Chargebee", "chargebee.com", "S2"), pick("Darwinbox", "darwinbox.com", "S1"),
                                       pick("Made Up Corp", "madeup.com", "S1"), pick("Zoho", "zoho.com", "S1")]}, "usage": {"usd": 0.015}}
    monkeypatch.setattr(discover, "search", fake_search)
    monkeypatch.setattr(discover, "exa_search", fake_exa)
    monkeypatch.setattr(discover, "fetch_page", fake_fetch)
    monkeypatch.setattr(discover, "ask_json", fake_ask)
    got = run(discover.find_companies({"icp": "SaaS companies in Bengaluru", "exclude": ["Zoho"]}))
    assert seen["category"] == "company" and "Exclude: Zoho" in seen["prompt"]
    # made-up names are dropped, an excluded one never comes back, and a domain no result mentions isn't shown
    assert [(c["name"], c["domain"]) for c in got["companies"]] == [("Chargebee", "chargebee.com"), ("Darwinbox", "")]
    assert got["usd"] == 0.022
    with pytest.raises(ValueError, match="Describe the companies"):
        run(discover.find_companies({"icp": "  "}))


def test_the_chat_lists_researched_companies_answers_from_research_and_keeps_history(monkeypatch, tmp_path):
    from agent import chat as chat_mod
    from agent import web as web_mod
    run_id = "13131313-1313-1313-1313-131313131313"
    write(tmp_path / f"{run_id}.json", {"run": {"run_id": run_id}, "campaign": {"generated_at": "2026-11-24T08:00:00.000Z", "icp": {"product": "Flow AI platform"}},
                                        "companies": [{"id": "c1", "name": "Acme", "domain": "acme.com", "fit_score": 45, "tier": "Nurture", "willingness_to_buy": "low",
                                                       "summary": "A wealth firm", "people": [], "review": {"decision": None}, "evidence": [], "financials": {},
                                                       "user_notes": {"text": "Budget review in March"}}]})
    listed = run(chat_mod.chat_companies({}, tmp_path))["companies"]
    assert [(c["name"], c["company_id"], c["messages"]) for c in listed] == [("Acme", "c1", 0)]
    sent = []

    async def fake_ask(system, context, messages, max_tokens=900):
        sent.append((context, messages))
        return {"text": f"Answer {len(sent)}", "usage": {"usd": 0.001}}

    async def fake_donsetch(query, max_results):
        return {"rows": [{"url": "https://news.example/acme", "title": "Acme expands", "snippet": "Acme opens a Dubai office."}], "cost": 0}

    async def fake_fetch(url, max_chars=6000):
        return None
    monkeypatch.setattr(chat_mod, "ask_text", fake_ask)
    monkeypatch.setitem(web_mod.engines, "donsetch", fake_donsetch)
    monkeypatch.setattr(web_mod, "fetch_page", fake_fetch)
    ask = {"run_id": run_id, "company_id": "c1"}
    assert run(chat_mod.chat(ask, tmp_path))["chat"] == [] and not sent  # opening a company costs nothing
    got = run(chat_mod.chat({**ask, "question": "How can they grow?"}, tmp_path))
    assert "A wealth firm" in sent[0][0] and "Budget review in March" in sent[0][0] and "Flow AI platform" in sent[0][0]
    assert [m["text"] for m in got["chat"]] == ["How can they grow?", "Answer 1"]
    got = run(chat_mod.chat({**ask, "question": "Any news?", "web": True}, tmp_path))
    assert "Dubai office" in sent[1][1][-1]["content"] and got["chat"][-1]["sources"] == ["https://news.example/acme"]
    assert [m["role"] for m in sent[1][1]] == ["user", "assistant", "user"]  # follow-ups carry the conversation
    for i in range(30):
        run(chat_mod.chat({**ask, "question": f"q{i}"}, tmp_path))
    assert len(sent[-1][1]) == chat_mod.HISTORY * 2 + 1 and len(run(chat_mod.chat(ask, tmp_path))["chat"]) == chat_mod.KEEP


def test_word_docx_files_are_read_and_old_doc_files_get_a_clear_message():
    import base64
    import io
    import zipfile
    from agent import pipeline
    doc = io.BytesIO()
    with zipfile.ZipFile(doc, "w") as z:
        z.writestr("word/document.xml", '<w:document><w:body><w:p><w:r><w:t>Indnord builds</w:t></w:r><w:r><w:t xml:space="preserve"> AI digital employees</w:t></w:r></w:p>'
                                        '<w:p><w:r><w:t>for banks &amp; telecoms</w:t></w:r></w:p></w:body></w:document>')
    b64 = lambda raw: base64.b64encode(raw).decode()
    assert pipeline.file_text("profile.docx", b64(doc.getvalue())) == "Indnord builds  AI digital employees\nfor banks & telecoms"
    for name, raw, why in [("profile.docx", b"not a zip", "isn't a real DOCX"), ("profile.doc", b"\xd0\xcf\x11\xe0", "Old .doc files"), ("x.exe", b"x", "PDF, PPTX, DOCX")]:
        with pytest.raises(ValueError, match=why):
            pipeline.file_text(name, b64(raw))


def test_the_company_profile_is_made_once_cached_and_written_as_a_word_file(monkeypatch, tmp_path):
    import io
    from docx import Document
    from agent import profile
    calls = []

    async def fake_ask(system, prompt, schema, **_):
        calls.append(prompt)
        return {"data": {"usable": True, "problem": "", "company": "Indnord", "summary": "Indnord builds FLOW.", "category": "AI operating system",
                         "offerings": [{"name": "FLOW", "what": "governed AI digital employees"}], "problems_solved": ["Slow delivery"],
                         "customers": {"industries": ["Banking"], "company_size": "Enterprise", "buyer_roles": ["CIO"], "regions": ["India"]},
                         "proof_points": ["35% lower TCO for a telecom"], "differentiators": [], "product_description": "FLOW deploys digital employees."},
                "usage": {"usd": 0.01}}
    monkeypatch.setattr(profile, "ask_json", fake_ask)
    body = {"product": "We sell FLOW, an AI operating system for enterprises."}
    first = run(profile.company_profile(body, tmp_path))
    again = run(profile.company_profile(body, tmp_path))
    assert len(calls) == 1 and again == first and first["sources"] == ["your product description"]  # same input: served from the cache, free
    text = "\n".join(p.text for p in Document(io.BytesIO(profile.profile_docx(first))).paragraphs)
    assert "Company profile: Indnord" in text and "FLOW: governed AI digital employees" in text and "Industries: Banking" in text
    assert "What sets you apart" not in text  # empty sections are left out
    with pytest.raises(ValueError, match="Describe your product"):
        run(profile.company_profile({}, tmp_path))
