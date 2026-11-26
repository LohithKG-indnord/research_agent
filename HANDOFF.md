# Handoff: Account Fit Agent (Lead Research Agent)

Start here. README.md has the full detail on using the app, the pipeline, leads and the database.

## What it does

The user describes what they sell (text, their website, or a PDF / PPTX / DOCX / text file) and names companies to target. For each company the agent:

1. Searches the web, confirms the right company and reads its pages.
2. Scores **fit** and **willingness to buy** (0–100, tiers Priority ≥75, Qualified ≥60, Nurture ≥40). Every claim must quote a fetched page word for word; unproven claims are capped.
3. Shows **financials** (revenue, growth, profit) only when the number is on the source page, plus an estimated **buying capacity**.
4. Writes a **growth plan** (business and technical ideas) and a **recommendation** (how the user's product helps), on demand.
5. Finds one **case study**: a similar, independent company that grew, with proof (on demand, uses Exa).
6. On accept, finds **Director level and above only** (C-level, founders, VPs, directors, heads; any company size) via Hunter or Apollo, and drafts outreach messages. Nothing is ever sent automatically.

Other features:

- An ICP tab ("Find companies") suggests 5 companies from a description.
- A floating chat assistant answers questions about researched companies.
- A Word download of "my company profile".
- Exports to Excel, PDF, CSV or JSON.

## Run it

Needs Windows or Mac with Python 3.12+, an Anthropic API key and an Exa API key.

```powershell
python -m pip install -r requirements.txt
copy .env.example .env   # then fill in the keys
python server.py         # http://localhost:3000 (set PORT=3001 if 3000 is taken)
```

- Command line: `python cli.py "Zoho, zoho.com" --product "..." [--criteria "..."] [--fresh]`
- Tests: `python -m pytest -q`. There are 40 tests; all are offline and free.

`.env` keys:

| Key | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | **Required.** |
| `CLAUDE_MODEL` | Model for judgement (research, scoring, messages, notes, growth plans). Defaults to `claude-sonnet-5-5` (Claude Sonnet 5.5). |
| `CHAT_MODEL` | Model for the chat assistant. Defaults to `claude-haiku-5-5`. |
| `EXA_API_KEY` | **Required for web search.** Exa's official API, about $0.007 per search. |
| `SEARCH_PROVIDER` / `PARALLEL_API_KEY` | Optional. `exa` (default) or `parallel` to use Parallel's Search API for research searches. Off by default; tested as a possible replacement (see README). |
| `CRAWLER_CONTACT` | A URL or email shown to website owners in the agent's User-Agent. Set it for production. |
| `HUNTER_API_KEY` / `APOLLO_API_KEY` | Lead lookups. Apollo needs a paid plan. |
| `SENDER_NAME` / `SENDER_TITLE` / `SENDER_COMPANY` | Signature for outreach messages. |
| `PRODUCT_DESCRIPTION` / `IDEAL_CUSTOMER` | Optional form defaults. |
| `PRIVACY_POLICY_URL` | Your privacy policy, linked in the notice at the end of every outreach message. Set it before sending anything. |
| `RETENTION_DAYS` | Days before leads are deleted automatically (default 365, minimum 30). |

Never commit `.env` or `leads/`; both are git-ignored.

## How the work is split

- **Free tools:** page reading (our own code; honest name, obeys `robots.txt`, paced per site) and the local ONNX reranker (`models/`, about 280 MB, downloaded once).
- **Claude (paid):** only *decides*: which company, which pages, the scores, the plans. It never browses.
- **Exa (paid):** all web search, through its official API. No search engine is scraped.

## Files

| File | What it does |
|---|---|
| `server.py` | FastAPI app. Serves `public/` and the `/api/*` routes, and accepts requests from localhost only. `/api/run` streams progress as NDJSON. |
| `cli.py` | Command-line run. |
| `agent/pipeline.py` | Main research run: identify → read pages → rerank → score → verify quotes and financials. Also reads uploaded files (`file_text`), the user's website (`product_material`), 7-day reuse (`recent_research`), the case study and `PROMPT_VERSION`. |
| `agent/people.py` | Accept/reject, lead lookup (Hunter/Apollo, seniority filter), outreach messages, notes re-scoring, growth plan. |
| `agent/llm.py` | Claude calls (`ask_json` with a schema, falling back when the schema is too large), prompt caching, cost tracking, `ask_text` for chat. |
| `agent/privacy.py` | GDPR duties for leads: privacy notice on every message, "Delete & don't contact" (erases everywhere, keeps only hashes in `leads/suppressed.json`), daily retention cleanup (`RETENTION_DAYS`). |
| `agent/roles.py` | Buying roles tab: role-based inference of each lead's buying role (8 roles, primary + secondary), 100-point authority/influence score, confidence capped by public evidence, buying motion (IT Software vs Services), committee map. One Claude call per company. |
| `agent/registry.py` | Legal entity and owners from GLEIF, the official LEI register (free, no key). Strict matching (no match rather than a wrong one), capped at 10 s, never fails a run. Owners feed the case-study "own group" filter. |
| `agent/web.py` | Search (Exa API) and polite page reading: honest User-Agent, `robots.txt` (RFC 9309) on every redirect hop, at least 1 s between requests per site, no private IPs, LinkedIn or social sites. |
| `agent/rerank.py` | Local cross-encoder that keeps the most relevant passages. |
| `agent/discover.py` | ICP "Find companies" (5 picks per call). |
| `agent/chat.py` | Chat assistant: company search, answers from saved research, optional web search (one Exa search per question). |
| `agent/profile.py` | "Download my company profile (Word)". Cached in `leads/profiles/`. |
| `agent/capacity.py` | Buying-capacity estimate from revenue (fixed shares: tech 3–7%, new offering 10–20%). |
| `agent/export.py` | Excel export. |
| `agent/db.py` | SQLite `leads/leads.db` (20 tables). JSON files in `leads/` are the main record; the DB is synced from them on start. |
| `agent/env.py`, `agent/util.py` | `.env` loading and small helpers. |
| `public/` | The whole UI (plain HTML, CSS and JS, no build step). |
| `tests/test_agent.py` | Offline tests; web and Claude are patched. |

## Cost (approximate)

| Action | Cost |
|---|---|
| Research, per company | $0.08–0.09 (2 Claude calls + 5 Exa searches) |
| Same company again within 7 days | Free (reused unless "Research again" is ticked) |
| Reading the user's website or document | About $0.01 once per run |
| Growth plan, case study, notes re-score | Each a few cents, only when clicked |
| Chat answer | About $0.001 |
| ICP find | $0.05–0.07 |
| Leads | Provider credits (Hunter 1 per email) |

Each result's `usage` field records the real cost.

## Known limits and gotchas

- **Unreachable websites** stop after 45 seconds with an error. Before that cap, a dead site could hang for minutes.
- **Websites that need JavaScript** have no readable text. The user should upload a document instead.
- **Port:** 3000 is the default;
- **Buying capacity** uses the same percentage ranges for every industry. Industry-specific ranges are a possible improvement.
- **Financials and case studies** are often "Unknown" or absent for small or private companies. This is by design: the agent shows nothing rather than an unverified figure.
- **Sites that block bots** in `robots.txt` (or whose `robots.txt` errors) are not read; those companies are scored from search snippets, so their scores can be lower.
- **EU / GDPR:** search queries hold company names, not personal data, but Exa is a US service: sign its data processing agreement. Leads (names, work emails) from Hunter/Apollo are personal data: built in: a privacy notice on every message, "Delete & don't contact" with a hashed do-not-contact list, and automatic deletion after `RETENTION_DAYS` (see README, Leads). Still your company's job: a documented lawful basis (usually a legitimate interest assessment), the privacy policy behind `PRIVACY_POLICY_URL`, and choosing the retention period.
- **On Windows**, the Claude desktop app's sandbox can hide files it writes under AppData. Keep tools inside the project folder.

## State at handoff

- **Repo:** https://github.com/LohithKG-indnord/research_agent (private).
- **Suggested next steps:**
  - Optionally add industry-based buying-capacity ranges.
  - Set `PRIVACY_POLICY_URL` and `RETENTION_DAYS` in `.env`.
