# Account Fit Agent

Say what you sell and name the companies you want to sell to. For each company the agent researches the web and tells you several things:

- how well the company fits your product, and how likely it is to buy;
- its financials and what it could spend;
- how it could grow, and how your product helps;
- who to contact, with a first message drafted.

Every claim is backed by a quote from a page the agent read. New here? Read [HANDOFF.md](HANDOFF.md) first.

## Run

You need Python 3.12+, an Anthropic API key and an Exa API key (web search).

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

Describe your ideal customer, for example "SaaS companies in Bengaluru with 50–500 staff", and the agent suggests 5 real companies with a reason for each. It uses three Exa searches (two for list pages, one limited to company websites) and one Claude call, about $0.05–0.07. Companies already suggested are excluded when you ask for more.

From the suggestions you can:

- send the selected companies to the research tab;
- copy them;
- clear the list (an "are you sure" bar asks first).

### Company cards

Each researched company gets a card with these tabs, in order:

1. **Fit & signals:** score and tier, willingness to buy, Accept/Reject, fit reasons, objections, buying signals, pitch angle and recommended next steps.
2. **Financials:** revenue, growth and profit. A figure is shown only when its number is on the source page (including table rows whose unit sits in the header); otherwise it says "Unknown". Newest year first; growth is calculated from two years when both are found. A **buying capacity** estimate follows: tech budget about 3–7% of revenue, new-offering budget about 10–20% of that, adjusted by willingness to buy.
3. **Growth plan:** business and technical ways the company could grow, independent of your product. Written when you click **Write growth plan** (about $0.02, fills this tab and Recommendation). Opening a tab or accepting a company never calls Claude on its own.
4. **Recommendation:** how your product addresses those points, other ways it can help, and the path to win. Also includes the **case study** button: one similar, independent company that grew, its revenue figures and what it implemented. Group companies and lookalike lists are excluded. If no proven case is found, nothing is shown.
5. **Your notes:** add facts the web couldn't verify. The agent then re-scores the company and rewrites the plan.
6. **Leads:** Director level and above only (see Leads below), each with Accept/Reject.
7. **Messages:** an email, a LinkedIn note and a follow-up for each accepted lead, with Copy, Open in Gmail and Rewrite. Nothing is sent automatically.
8. **Evidence:** the quotes behind the scores (collapsed by default).

**Download** (top of the results) exports company research or accepted leads as Excel, a PDF report (print or save as PDF), CSV or JSON.

### Chat assistant

The **Ask about a company** button opens a side panel. Search for one of your researched companies, then ask questions, such as how they could grow or whether your product fits. Answers come from the saved research and cite their sources.

- It uses Claude Haiku 5.5 (`claude-haiku-5-5`), about $0.001 per answer.
- "Search the web too" adds one Exa search and reads the top two pages, about $0.007 more per answer.
- The last 6 exchanges are sent as context, and chats are saved with the company.

## How research works (per company, 3 in parallel)

1. **Search and identify.** Three searches: the official website, news and funding, and topics tied to your product. Claude reads only the result list, confirms the company and its domain, and picks pages. Claude never browses.
2. **Read.** The agent fetches the home, about, careers, products, news and customers pages plus the picked pages. Two more searches look for revenue/profit for the latest years and for funding/employees.
3. **Rerank.** A local cross-encoder (bge-reranker-base, ONNX) keeps the 18 passages most relevant to your product, plus up to 8 financial passages.
4. **Score.** Claude scores the company from those passages only and must quote them word for word.
5. **Verify.** Quotes that don't match the fetched text are marked inferred and don't count. A dimension with no verified quote is capped at 35, and financial figures must appear on their page.

Score weights (`WEIGHTS` in `agent/pipeline.py`): has the problem 30, profile fit 20, timing 20, buyer visible 15, evidence quality 15. Tiers: Priority ≥75, Qualified ≥60, Nurture ≥40. No company is dropped; low scorers get a growth plan to show how they could become buyers.

**Search.** All web search goes through Exa's official API (its own index, licensed for automated use, about $0.007 per search). The agent never scrapes a search engine. Without `EXA_API_KEY` there is no search, and a company is researched only from a website you give.

**Optional: Parallel search (off by default).** Set `SEARCH_PROVIDER=parallel` and `PARALLEL_API_KEY` in `.env` to send research searches to Parallel's official Search API instead ("basic" mode, about $0.005 per search; its EU endpoint supports this mode). Remove the line or set it to `exa` to switch back. The ICP tab's company-only search always uses Exa. In a 25-search test on 5 past companies, Parallel returned rich text for 97% of results (Exa: 74%), found every official website (Exa: 4 of 5) and cost 29% less, but took about 0.6 s longer per search. A full research comparison hasn't been run yet.

**Official register (GLEIF).** While pages are read, the agent looks the company up in GLEIF, the official global register of Legal Entity Identifiers (open data, free, no key). The lookup has three effects:

- When it finds the company, the card shows the registered legal name and country, the owner when the register records one, and a link to the record.
- The registered name and owners are added to the "own group" list, so the case study never picks the company's own group.
- A match must be unambiguous: the brand plus a legal suffix, or plus one word such as "Technologies"; an entity in the company's own country wins.
  - A business line ("PayU Finance"), a subsidiary ("Swiggy Instamart") or a tie it can't break gives no match, rather than a wrong one.

The lookup is capped at 10 seconds. If the register is slow or down, research carries on without it.

**Reading websites.** The agent reads public pages the way a well-behaved crawler does:

- It names itself honestly (`AccountFitAgent/1.0`, plus `CRAWLER_CONTACT` from `.env`) and never pretends to be a browser.
- It obeys each site's `robots.txt` (RFC 9309): pages a site disallows are never requested. A site whose `robots.txt` returns a server error is not read at all. A missing `robots.txt` means no rules. Rules are cached for up to 24 hours.
- It waits at least 1 second between requests to the same site, or longer if the site's `Crawl-delay` asks. A site asking for more than 10 seconds is skipped.
- A "429 slow down" reply is not retried.
- One page gets at most 25 seconds, retries included, so a stalled site can't hold up the run.
- A page it may not read is scored from its search snippet instead.

**Claude.**

- `CLAUDE_MODEL` makes the judgement calls (research, scoring, messages, notes, growth plans). Default `claude-sonnet-5-5` (Claude Sonnet 5.5).
- `CHAT_MODEL` answers chat questions (default `claude-haiku-5-5`).
- Calls use structured JSON output. If a schema is refused as "too large", the call falls back to a plain prompt.
- System prompts are cached.

**Cost.**

| Item | Cost |
|---|---|
| Research, per company | About $0.08–0.09 (2 Claude calls + 5 Exa searches) |
| Reading your website or document | About $0.01 once per run |
| Growth plan, case study, notes re-score | A few cents each, only when used |

Each result records tokens, searches and cost under `usage`.

**Safety.**

- Page fetches skip LinkedIn, other social networks and private network addresses, on every redirect.
- Page and document content is treated as data, never as instructions.
- Only public pages are read; nothing behind a login, and `robots.txt` is obeyed (see Reading websites).
- The server only accepts requests from this computer.
- Search queries (company names and short topics, no personal data) go to Exa, a US service. Sign Exa's data processing agreement and check its terms before production use.

## Leads

Accepting a company looks up its buyers with the first provider that has a key: Hunter, then Apollo.

**Who is returned:**

- Director level and above, at every company: C-level, founders, owners, presidents, partners, VPs, directors and heads.
- Junior titles are filtered out, including people found in earlier lookups.

**Credits:**

- Apollo filters titles *before* unlocking contacts, so filtered-out people cost nothing.
- Hunter asks for executives only, and adds a department search when the title search finds fewer than 5 people.
- At most 5 people per lookup.
- A repeated search is never charged again, and "Find more leads" returns only people not seen before.
- The Leads tab shows all leads, including earlier ones, by default.
- Hunter's free plan works (1 credit per email). Apollo's people API needs a paid plan.

**Messages:** accepting a lead writes an email, a LinkedIn connection note (under 300 characters) and a follow-up, signed with `SENDER_*` from `.env`. One small Claude call per lead, never repeated.

**Privacy:**

- Personal emails and phone numbers are never requested.
- LinkedIn links are copied, never opened.
- Nothing is sent.

**Buying roles** (a switch inside the Leads tab). **Classify buying roles** sorts the company's leads by role-based inference. It is one Claude call, about $0.02–0.03, and runs only when clicked. The result is a table with one row per lead, showing:

- **Roles:** a primary and a secondary buying role out of eight: Initiator, Influencer, Champion, Decision Maker, Economic Buyer, Buyer / Procurement, Gatekeeper, End User.
- **Confidence:** low, medium or high.
- **CRM fields:** decision authority, technical influence, budget ownership, business interest, engagement status.
- **Likely entry point**, marked on the person most likely to own the problem.

Hovering a name shows the one-line reason. Rows are ordered by an authority / influence score out of 100: budget ownership 25, decision authority 25, functional ownership 20, technical/business influence 20, internal advocacy 10, each scored 0–4. The score is stored but not shown. The view also shows the buying motion (IT Software or IT Services) and a buying committee map, with the titles to look for in roles no lead fills yet.

These are starting hypotheses, not confirmed roles. They use only the lead's title and the company research, with no LinkedIn reading and no extra searching about the person. **High confidence** requires at least two verified quotes in the research that name the person, and the code enforces that. Leads are looked up at Director level and above, so architects, procurement staff and end users often appear as "not found". Deleting a lead also deletes their classification.

**GDPR safeguards (`agent/privacy.py`):**

- **Privacy notice (Article 14).** Every outreach email and LinkedIn follow-up ends with a line saying:
  - where the person's work details came from (Hunter.io or Apollo.io);
  - that they can reply "no" to opt out;
  - your privacy policy link (`PRIVACY_POLICY_URL`).

  The line is added in code, never by Claude, so it can't be left out or reworded. Messages written before this existed get it at the next server start. Until `PRIVACY_POLICY_URL` is set, the line shows a reminder in place of the link. LinkedIn connection notes have no room (300 characters), so the follow-up carries the notice.
- **Delete & don't contact (Articles 17 and 21).** Each lead has a "Delete & don't contact" button; it asks twice. It erases the person from:
  - every saved run (`leads/*.json`, any case of email);
  - the database, including their sources;
  - any raw Hunter/Apollo reply that names them.

  It then adds them to `leads/suppressed.json`. That file holds only one-way hashes of their email and of name + company, so it can block them without storing who they are, and no later lookup adds them back.
- **Retention (Article 5).** Leads are deleted `RETENTION_DAYS` (default 365, minimum 30) after they were found. A contacted lead is deleted that long after the contact. Raw Hunter/Apollo replies older than that go too. The cleanup runs when the server starts and then daily.

Still needed from your side:
- a documented lawful basis (usually a legitimate interest assessment);
- the privacy policy the link points to;
- a decision on the retention period;
- checking the provider terms.

Someone qualified should confirm these.

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
