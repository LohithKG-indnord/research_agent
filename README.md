# Account Fit Agent

Name the companies you want to sell to. For each one, the agent researches it on the web and judges whether it fits **your product** and is likely to buy. Every score comes with quotes from the pages it read.

## Run

Python 3.12+.

```powershell
python -m pip install -r requirements.txt
python server.py
```

Open http://localhost:3000. It needs `ANTHROPIC_API_KEY` in `.env` (see `.env.example`). The reranker model is kept in `models/` (about 280 MB; downloaded once if missing). Put your product in `PRODUCT_DESCRIPTION` in `.env` and the form fills it in for you.

Command line:

```powershell
python cli.py "Razorpay" "Zoho, zoho.com" --product "AI voice agent for inbound support calls"
```

Asking for leads again ("Find more leads") returns only people not seen before for that company, in this run or earlier runs; Hunter is paged through, so it never charges again for the same people. The card shows the new leads by default, with an option to show all. Accepting a lead also writes a short personal email, a LinkedIn connection note (kept under 300 characters: a longer one is rewritten once, not cut) and a LinkedIn follow-up from the research (one small Claude call, about $0.005, never repeated), signed with SENDER_NAME / SENDER_TITLE / SENDER_COMPANY from `.env`. They appear in the card's Messages tab, editable, with Copy, Open in Gmail, Open in email app and Open LinkedIn profile buttons, a Rewrite box ("shorter", "mention their hiring"; about $0.005 each) and Mark sent / Skip to track outreach; nothing is sent automatically. When a company name could mean several companies, the card lists the others so you can research the right one. Each lead found has Accept and Reject buttons; only accepted leads go into `leads.db` and the accepted leads CSV, and rejecting a lead removes it from the database.

Web search: DonSeTch (free) runs first. Exa (paid) is called only when DonSeTch fails or returns fewer than half the wanted results that mention the company; the results are then merged, those about the company first. The 2 financial searches always use both (about $0.014 per company), because Exa's longer page extracts often hold the figures.

Every run is saved to `leads/leads.json` (latest) and `leads/<run_id>.json`, and to the database `leads/leads.db` (see Database).

## How it works (per company, 3 in parallel)

This is "named-company research" from the Lead Research Agent design doc (section 4.2).

1. **Search and identify.** Three searches run through Exa (if `EXA_API_KEY` is set) or DonSeTch. Claude reads only the short result list, confirms the domain and picks up to 8 pages. Claude never searches the web; it only decides from what they find. If both return nothing and you gave no readable website, that company fails with a clear message and no Claude call is made. The company name and the first words of your product go to the search provider.
2. **Read.** The agent fetches the company's home, about, careers, products, news and customers pages, plus the picked pages. A page it can't read counts by its search snippet.
   Two more searches look for revenue, revenue growth, profit, funding and employees. Up to 6 passages with such figures are added for Claude. A figure is kept only if its value is word for word in the quote and in the source, and it belongs to the company itself; otherwise it is "Unknown". Other figures found (another source or year) are shown next to it. Figures don't change the score.
3. **Rerank passages.** The pages are split into passages. A local cross-encoder (BAAI bge-reranker-base, ONNX via onnxruntime) keeps the 18 passages most relevant to your product and to buying signals.
4. **Qualify.** Claude scores the company using **only** those passages. It gives a willingness-to-buy verdict, a pitch angle, objections, buying signals, who to approach and next steps, and must quote the sources word for word.
5. **Verify.** Each quote is checked against the fetched text. Quotes that don't match are marked `inferred` and don't count. A dimension with no verified quote is capped at 35. Evidence quality is calculated from the verified quotes. If the agent isn't sure it found the right company, the score is lowered.

Final score weights (design doc section 7, `WEIGHTS` in `agent/pipeline.py`): has the problem 30, ICP fit 20, buying signals 20, buyer visible 15, evidence quality 15. Tiers: Priority ≥75, Qualified ≥60, Nurture ≥40. Companies are ranked by score, and none are dropped.

Safety rules: page fetches skip LinkedIn, other social networks and private addresses, on every redirect. Page content is data, never instructions. `robots.txt` is not checked. DonSeTch (AGPL-3.0, separate program, `npm install -g donsetch`) is used for search only; it scrapes public search engines, so review its licence and those terms before production.

Each result records tokens, searches and cost under `usage`.

## Leads

Accepting a company on its card looks up its buyers straight away with the first provider that has a key in `.env` (Hunter, then Apollo). It searches the job titles the agent chose for your product (`buyer_titles`, e.g. "Head of Customer Support"); runs made before these titles existed get them from one small Claude call (about $0.002), saved so it never repeats. "Find leads" retries with another provider or with titles you type. Apollo's free plan blocks its people API, so Apollo needs a paid plan; Hunter's free plan works (50 credits a month, 1 per email returned, none when nothing matches or a search repeats). To save credits, Hunter is asked only for named people with a job title; if no one has a buyer title, it returns at most 3 decision makers instead. Nothing is looked up for a company you haven't accepted. Each lookup returns at most 5 people and repeats of the same search are not paid for again. Personal emails and phone numbers are never requested, LinkedIn links are copied and never opened, and nothing is sent to anyone. "Download leads CSV" exports people at accepted companies. The server only accepts connections from this computer.

There is no delete or suppression screen yet. To erase a person, remove them from the JSON files in `leads/` and from `leads.db`: their `people` row, and any `provider_responses` payload that names them. Check each provider's terms and the privacy law that applies before contacting people.

## Database

`leads/leads.db` (SQLite) holds the tables of the design doc, sections 8 and 8.2: workspaces, users, workspace_members, research_briefs, search_jobs, provider_runs, provider_responses, companies, people, company_people, company_rankings, person_rankings, buying_signals, evidence, company_sources, person_sources, review_decisions, leads, enrichment_history. `company_financials` is added. The JSON file stays the primary contract; each save updates the database, and starting the server brings it in line with every file in `leads/`.

Differences from the doc: JSON is stored as checked TEXT instead of JSONB with GIN indexes; there is one organisation and no row-level security, because SQLite has none; `person_rankings` stays empty because people are not scored; `retention_class` is "unset" until Q7 is decided; fetched web pages are kept only as evidence excerpts.

Open it with any SQLite viewer. Accepted companies and their people:

```sql
SELECT c.name, r.final_score, p.name AS person, cp.title, p.email
FROM leads l JOIN companies c ON c.id = l.company_id JOIN company_rankings r ON r.id = l.ranking_id
LEFT JOIN company_people cp ON cp.company_id = c.id LEFT JOIN people p ON p.id = cp.person_id
WHERE l.status = 'accepted';
```

## Test

```powershell
python -m pytest
```

The earlier Node.js version is kept, unused, in `legacy_js/`. It can be deleted.
