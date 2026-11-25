# Account Fit Agent

Say what you sell and name the companies you want to sell to. For each company the agent researches the web and tells you several things:

- how well the company fits your product, and how likely it is to buy;
- its financials and what it could spend;
- how it could grow, and how your product helps;
- who to contact, with a first message drafted.

Every claim is backed by a quote from a page the agent read. New here? Read [HANDOFF.md](HANDOFF.md) first.

## Run

You need Python 3.12+. For free web search, also install DonSeTch: `npm install -g donsetch`.

```powershell
python -m pip install -r requirements.txt
copy .env.example .env
python server.py
```

Open http://localhost:3000. If that port is taken, set `PORT=3001`. The only required key is `ANTHROPIC_API_KEY`; see `.env.example` for the others. The first start downloads the reranker model into `models/` (about 280 MB).

Command line:

```powershell
python cli.py "Razorpay" "Zoho, zoho.com" --product "AI voice agent for inbound support calls" --criteria "200-5,000 staff, India" --fresh
```

Tests (offline and free):

```powershell
python -m pytest -q
```

## Using the app

### Research companies tab

- **Companies to research:** one per line. Add a domain after a comma if the name is ambiguous.
- **Your product:** any of three inputs: a text description, your website, or a deck or document. Files can be PDF, PPTX, DOCX or text, up to 10 MB, and may be about your company or your product.
  - The agent reads the material and writes the product description it researches with (one small Claude call).
  - It refuses material that isn't about a company or what it sells (for example a resume or an invoice) and says why.
  - It also refuses unreadable files: scanned PDFs, old `.doc` files, encrypted files.
  - It gives up on a website that doesn't answer within 45 seconds.
- **Download my company profile (Word):** a `.docx` of what the agent understood about your company: what you sell, the problems you solve, who buys, proof points and differentiators. About $0.01; free when the input hasn't changed (cached in `leads/profiles/`).
- **Ideal customer:** optional.
- **Research again:** research done in the last 7 days with the same product and ideal customer is normally reused at no cost. Tick this box to force a fresh run.

### Find companies (ICP) tab

Describe your ideal customer, for example "SaaS companies in Bengaluru with 50–500 staff", and the agent suggests 5 real companies with a reason for each. It uses two free searches, one Exa company search and one Claude call, about $0.02–0.03. Companies already suggested are excluded when you ask for more.

From the suggestions you can:

- send the selected companies to the research tab;
- copy them;
- clear the list (an "are you sure" bar asks first).

### Company cards

Each researched company gets a card with these tabs, in order:

1. **Fit & signals:** score and tier, willingness to buy, Accept/Reject, fit reasons, objections, buying signals, pitch angle and recommended next steps.
2. **Financials:** revenue, growth and profit. A figure is shown only when its number is on the source page (including table rows whose unit sits in the header); otherwise it says "Unknown". Newest year first; growth is calculated from two years when both are found. A **buying capacity** estimate follows: tech budget about 3–7% of revenue, new-offering budget about 10–20% of that, adjusted by willingness to buy.
3. **Growth plan:** business and technical ways the company could grow, independent of your product. Written when you first open the tab.
4. **Recommendation:** how your product addresses those points, other ways it can help, and the path to win. Also includes the **case study** button: one similar, independent company that grew, its revenue figures and what it implemented. Group companies and lookalike lists are excluded. If no proven case is found, nothing is shown.
5. **Your notes:** add facts the web couldn't verify. The agent then re-scores the company and rewrites the plan.
6. **Leads:** decision makers only (see Leads below), each with Accept/Reject.
7. **Messages:** an email, a LinkedIn note and a follow-up for each accepted lead, with Copy, Open in Gmail and Rewrite. Nothing is sent automatically.
8. **Evidence:** the quotes behind the scores (collapsed by default).

**Download** (top of the results) exports company research or accepted leads as Excel, a PDF report (print or save as PDF), CSV or JSON.

### Chat assistant

The **Ask about a company** button opens a side panel. Search for one of your researched companies, then ask questions, such as how they could grow or whether your product fits. Answers come from the saved research and cite their sources.

- It uses Haiku, about $0.001 per answer.
- "Search the web too" adds free DonSeTch results.
- The last 6 exchanges are sent as context, and chats are saved with the company.

## How research works (per company, 3 in parallel)

1. **Search and identify.** Three searches: the official website, news and funding, and topics tied to your product. Claude reads only the result list, confirms the company and its domain, and picks pages. Claude never browses.
2. **Read.** The agent fetches the home, about, careers, products, news and customers pages plus the picked pages. Two financial searches (revenue/profit for the latest years, funding/employees) always use DonSeTch and Exa.
3. **Rerank.** A local cross-encoder (bge-reranker-base, ONNX) keeps the 18 passages most relevant to your product, plus up to 8 financial passages.
4. **Score.** Claude scores the company from those passages only and must quote them word for word.
5. **Verify.** Quotes that don't match the fetched text are marked inferred and don't count. A dimension with no verified quote is capped at 35, and financial figures must appear on their page.

Score weights (`WEIGHTS` in `agent/pipeline.py`): has the problem 30, profile fit 20, timing 20, buyer visible 15, evidence quality 15. Tiers: Priority ≥75, Qualified ≥60, Nurture ≥40. No company is dropped; low scorers get a growth plan to show how they could become buyers.

**Search.** DonSeTch (free) runs first. Exa (paid, about $0.007 per search) is added only when DonSeTch finds too little, and always for the financial and case-study searches.

**Claude.**

- `CLAUDE_MODEL` makes the judgement calls. The code default is `claude-opus-5-5`; `claude-sonnet-5` is cheaper and works well.
- `CHAT_MODEL` answers chat questions (default `claude-haiku-5-5`).
- Calls use structured JSON output. If a schema is refused as "too large", the call falls back to a plain prompt.
- System prompts are cached.

**Cost.**

| Item | Cost |
|---|---|
| Research, per company | About $0.06–0.07 (2 Claude calls + a few Exa searches) |
| Reading your website or document | About $0.01 once per run |
| Growth plan, case study, notes re-score | A few cents each, only when used |

Each result records tokens, searches and cost under `usage`.

**Safety.**

- Page fetches skip LinkedIn, other social networks and private network addresses, on every redirect.
- Page and document content is treated as data, never as instructions.
- `robots.txt` is not checked.
- The server only accepts requests from this computer.
- DonSeTch is AGPL-3.0 and sends each query to public search endpoints without a key (Google's search widget, Keenable, Exa's and Parallel's public MCP servers), mostly US services with no data processing agreement. Review its licence and their terms before production use.

## Leads

Accepting a company looks up its buyers with the first provider that has a key: Hunter, then Apollo.

**Who is returned:**

- Only decision makers: C-level, founders, owners, partners and VPs.
- Directors and heads too, but only at companies with 1,000+ staff.
- Junior titles are filtered out, including people found in earlier lookups.

**Credits:**

- Apollo filters titles *before* unlocking contacts, so filtered-out people cost nothing.
- Hunter asks for executives only, and runs its department search only for large companies.
- At most 5 people per lookup.
- A repeated search is never charged again, and "Find more leads" returns only people not seen before.
- The Leads tab shows all leads, including earlier ones, by default.
- Hunter's free plan works (1 credit per email). Apollo's people API needs a paid plan.

**Messages:** accepting a lead writes an email, a LinkedIn connection note (under 300 characters) and a follow-up, signed with `SENDER_*` from `.env`. One small Claude call per lead, never repeated.

**Privacy:**

- Personal emails and phone numbers are never requested.
- LinkedIn links are copied, never opened.
- Nothing is sent.

There is no delete screen yet. To erase a person, remove them from the JSON files in `leads/` and from `leads.db`: the `people` row and any `provider_responses` payload that names them. Check the provider terms and the privacy law that applies before contacting people.

## Storage and database

Every run is saved to `leads/leads.json` (latest) and `leads/<run_id>.json`, and mirrored into `leads/leads.db` (SQLite). The JSON files are the primary record; starting the server brings the database in line with them. Company profiles are cached in `leads/profiles/`. `leads/` and `.env` are git-ignored and must never be committed.

The database has 20 tables:

- workspaces, users, workspace_members;
- research_briefs, search_jobs, provider_runs, provider_responses;
- companies, people, company_people;
- company_rankings, person_rankings, buying_signals, evidence;
- company_sources, person_sources, company_financials;
- review_decisions, leads, enrichment_history.

Extra per-company fields are stored in the `details` JSON column of `company_rankings`: growth plan, recommendation, case study, buying capacity, notes and chat.

Differences from the design doc:

- JSON is stored as checked TEXT.
- There is one organisation and no row-level security.
- `person_rankings` stays empty.
- Fetched pages are kept only as evidence excerpts.

Accepted companies and their people:

```sql
SELECT c.name, r.final_score, p.name AS person, cp.title, p.email
FROM leads l JOIN companies c ON c.id = l.company_id JOIN company_rankings r ON r.id = l.ranking_id
LEFT JOIN company_people cp ON cp.company_id = c.id LEFT JOIN people p ON p.id = cp.person_id
WHERE l.status = 'accepted';
```

The earlier Node.js version is kept, unused, in `legacy_js/`. It can be deleted.
