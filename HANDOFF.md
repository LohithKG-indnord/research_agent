# Handoff: Account Fit Agent (Lead Research Agent)

Start here. README.md has the full detail on using the app, the pipeline, leads and the database.

## What it does

The user describes what they sell (text, their website, or a PDF / PPTX / DOCX / text file) and names companies to target. For each company the agent:

1. Searches the web, confirms the right company and reads its pages.
2. Scores **fit** and **willingness to buy** (0–100, tiers Priority ≥75, Qualified ≥60, Nurture ≥40). Every claim must quote a fetched page word for word; unproven claims are capped.
3. Shows **financials** (revenue, growth, profit) only when the number is on the source page, plus an estimated **buying capacity**.
4. Writes a **growth plan** (business and technical ideas) and a **recommendation** (how the user's product helps), on demand.
5. Finds one **case study**: a similar, independent company that grew, with proof (on demand, uses Exa).
6. On accept, finds **decision makers only** (C-level, founders, VPs; directors only at 1,000+ staff) via Hunter or Apollo, and drafts outreach messages. Nothing is ever sent automatically.

Other features:

- An ICP tab ("Find companies") suggests 5 companies from a description.
- A floating chat assistant answers questions about researched companies.
- A Word download of "my company profile".
- Exports to Excel, PDF, CSV or JSON.

## Run it

Needs Windows or Mac with Python 3.12+. For free search, install DonSeTch with `npm install -g donsetch`.

```powershell
python -m pip install -r requirements.txt
copy .env.example .env   # then fill in the keys
python server.py         # http://localhost:3000 (set PORT=3001 if 3000 is taken)
```

- Command line: `python cli.py "Zoho, zoho.com" --product "..." [--criteria "..."] [--fresh]`
- Tests: `python -m pytest -q`. There are 34 tests; all are offline and free.

`.env` keys:

| Key | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | **Required.** |
| `CLAUDE_MODEL` | Model for judgement. The code defaults to `claude-opus-5-5`; this project has been run with `claude-sonnet-5`, which is cheaper. |
| `CHAT_MODEL` | Model for the chat assistant. Defaults to `claude-haiku-5-5`. |
| `EXA_API_KEY` | Paid search, about $0.007 per search, used only as a top-up. |
| `HUNTER_API_KEY` / `APOLLO_API_KEY` | Lead lookups. Apollo needs a paid plan. |
| `SENDER_NAME` / `SENDER_TITLE` / `SENDER_COMPANY` | Signature for outreach messages. |
| `PRODUCT_DESCRIPTION` / `IDEAL_CUSTOMER` | Optional form defaults. |

Never commit `.env` or `leads/`; both are git-ignored.

## How the work is split

- **Free tools:** DonSeTch (a local search binary), page fetching, and the local ONNX reranker (`models/`, about 280 MB, downloaded once).
- **Claude (paid):** only *decides*: which company, which pages, the scores, the plans. It never browses.
- **Exa (paid):** tops up search only when DonSeTch finds too little, plus the financial and case-study searches.

## Files

| File | What it does |
|---|---|
| `server.py` | FastAPI app. Serves `public/` and the `/api/*` routes, and accepts requests from localhost only. `/api/run` streams progress as NDJSON. |
| `cli.py` | Command-line run. |
| `agent/pipeline.py` | Main research run: identify → read pages → rerank → score → verify quotes and financials. Also reads uploaded files (`file_text`), the user's website (`product_material`), 7-day reuse (`recent_research`), the case study and `PROMPT_VERSION`. |
| `agent/people.py` | Accept/reject, lead lookup (Hunter/Apollo, seniority filter), outreach messages, notes re-scoring, growth plan. |
| `agent/llm.py` | Claude calls (`ask_json` with a schema, falling back when the schema is too large), prompt caching, cost tracking, `ask_text` for chat. |
| `agent/web.py` | Search (DonSeTch, Exa), safe page fetch (blocks private IPs, LinkedIn and social sites on every redirect). |
| `agent/rerank.py` | Local cross-encoder that keeps the most relevant passages. |
| `agent/discover.py` | ICP "Find companies" (5 picks per call). |
| `agent/chat.py` | Chat assistant: company search, answers from saved research, optional free web search. |
| `agent/profile.py` | "Download my company profile (Word)". Cached in `leads/profiles/`. |
| `agent/capacity.py` | Buying-capacity estimate from revenue (fixed shares: tech 3–7%, new offering 10–20%). |
| `agent/export.py` | Excel export. |
| `agent/db.py` | SQLite `leads/leads.db` (20 tables). JSON files in `leads/` are the main record; the DB is synced from them on start. |
| `agent/env.py`, `agent/util.py` | `.env` loading and small helpers. |
| `public/` | The whole UI (plain HTML, CSS and JS, no build step). |
| `tests/test_agent.py` | Offline tests; web and Claude are patched. |
| `legacy_js/` | The earlier Node version. Not used; kept for reference. |

## Cost (approximate)

| Action | Cost |
|---|---|
| Research, per company | $0.06–0.07 (2 Claude calls + a few Exa searches) |
| Same company again within 7 days | Free (reused unless "Research again" is ticked) |
| Reading the user's website or document | About $0.01 once per run |
| Growth plan, case study, notes re-score | Each a few cents, only when clicked |
| Chat answer | About $0.001 |
| ICP find | $0.02–0.03 |
| Leads | Provider credits (Hunter 1 per email) |

Each result's `usage` field records the real cost.

## Known limits and gotchas

- **Unreachable websites** stop after 45 seconds with an error. Before that cap, a dead site could hang for minutes.
- **Websites that need JavaScript** have no readable text. The user should upload a document instead.
- **Port:** 3000 is the default; 3001 was used locally because Docker held 3000 (`.claude/launch.json` has both).
- **Buying capacity** uses the same percentage ranges for every industry. Industry-specific ranges are a possible improvement.
- **Financials and case studies** are often "Unknown" or absent for small or private companies. This is by design: the agent shows nothing rather than an unverified figure.
- **Deleting a person** has no screen. Remove them from the `leads/` JSON files and `leads.db` by hand (see README).
- **DonSeTch** (AGPL) scrapes public search engines. Review its licence and terms before production use.
- **On Windows**, the Claude desktop app's sandbox can hide files it writes under AppData. Keep tools inside the project folder.

## State at handoff

- **Repo:** https://github.com/LohithKG-indnord/research_agent (private). Only the first commit (`2124e41`) is pushed.
- **Not yet committed:** the chat assistant, chat UI and search, DOCX upload, the company profile download, the website timeout and several UI fixes. Review them and commit them before handing over the repo.
- **Suggested next steps:**
  - Commit the pending work.
  - Optionally add industry-based buying-capacity ranges.
  - Add a delete/suppression screen for people.
