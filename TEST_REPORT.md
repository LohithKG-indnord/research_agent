# Test report: Account Fit Agent

Date: 26 Nov 2026 · Tested on Windows 11, Python 3.12, local server on port 3000.

## Verdict

**Ready for production on one computer, for one user**, once the four settings under "Before you go live" are done.

**Not ready to host for a team on a shared server as it is.** There is no login, results are stored as files on that computer, and the server only answers that computer by design.

## What was tested and the results

| Area | Checks | Result |
|---|---|---|
| Automated test suite | 39 tests covering scoring, quote checks, financials, search, polite page reading, the official register, leads, messages, GDPR delete and retention, buying roles, exports and file reading | ✅ 39/39 pass |
| Code checks | Python compile, linter (undefined names, unused code), page script syntax | ✅ 2 cosmetic warnings found and fixed |
| Access control | Other websites can't reach the server (Host check), cross-site form posts refused, API docs hidden, source files, `.env` and the database can't be downloaded | ✅ 7/7 |
| Bad input on every API route | Broken JSON, wrong types, oversized bodies, path tricks in IDs, empty and over-long input, unknown runs, companies and leads | ✅ 20/20 give a clean error, no crashes |
| Unsafe links (SSRF) | Own machine, internal network, cloud metadata address, `file://`, LinkedIn | ✅ 6/6 refused |
| File uploads | Over 10 MB, old `.doc`, image renamed `.pdf`, damaged PDF, fake PPTX, empty text, `.exe`, corrupt upload | ✅ 8/8 refused with a clear message |
| Export safety | Spreadsheet formulas (`=`, `+`, `@`) in Excel and CSV are stored as plain text, so they never run when opened | ✅ |
| Page safety (XSS) | Every place web data is shown on the page was checked for escaping | ✅ No unescaped web data found |
| Secrets | No API keys in git; `.env`, `leads/`, the database and local settings are git-ignored | ✅ |
| Page in the browser | Every tab on every company, Leads ↔ Buying roles switch, ICP panel, chat open/close: no errors, no empty tabs, and **opening tabs makes no paid calls** | ✅ |
| Phone width (375 px) | No sideways scrolling in any view; chat goes full screen | ✅ |
| Live paid run (earlier today) | One company end to end with Exa search, polite reading, the official register, verified financials: $0.085 | ✅ Worked, but took 255 s, so a 25 s per-page limit was added |

## Fixed during testing

- **Unpinned libraries.** `requirements.txt` used `>=`, so a future install could pull a newer version that breaks things. Every library is now pinned to the exact version tested; the dry-run install and tests pass.
- **Two lint warnings:** an unused import and an unnecessary `f` prefix.

## Before you go live

1. `PRIVACY_POLICY_URL` in `.env`. Until it's set, every outreach message ends with a reminder in place of the link. **Don't send messages before this.**
2. `CRAWLER_CONTACT` in `.env` (a company URL or email). It's shown to website owners, and some sites (for example Wikimedia) refuse bots without it.
3. `RETENTION_DAYS` in `.env`: your chosen retention period (default 365).
4. Paperwork only your company can do: a legitimate interest assessment, the privacy policy page, and a data processing agreement with Exa (a US service).

## Known limits (not bugs)

- **Single user, single machine.** No login, local only, files plus SQLite. Hosting for a team would need login, a real database and HTTPS.
- **Speed.** About 1–2 minutes per company; a slow website can add up to 25 s per page.
- **Scoring is only as good as the public web.** Small private companies often get "Unknown" financials and no case study, by design: the agent shows nothing rather than guess.
- **Buying roles** come from job titles only, so most leads are "low confidence" until confirmed in conversation.
- **Lead lookup** returns Director level and above only, so procurement, architects and end users usually show as "not found".

## Paid end-to-end run (after all changes)

One company (Freshworks) through the app's own API, exactly as the buttons do it. **9/9 passed, $0.25 in total.**

| Step | Result | Time | Cost |
|---|---|---|---|
| Research | 45/100 Nurture · 6 verified quotes · Exa search · registered as FRESHWORKS INC. · revenue $838.8M, growth 16%, funding $484M, 7.8K staff · no growth plan written yet ✅ | 69 s | $0.087 |
| Accept | Looked up leads (1 found: CEO) · **no Claude call** ✅ | 2 s | Hunter credits |
| Accept a lead | Message written · privacy notice in the email and the follow-up · LinkedIn note 201/300 characters | 6 s | $0.009 |
| Write growth plan | 5 business + 5 technical ideas | 11 s | $0.018 |
| Find a case study | No proven case study (shown as none, by design) · similar: Zendesk, Sierra | 33 s | $0.052 |
| Classify buying roles | CEO → Economic Buyer, medium confidence (named in a quote) · IT Services | 5 s | $0.012 |
| Chat question | Sensible, sourced answer | 2 s | $0.001 |
| ICP: find companies | Kissflow, Gofrugal, Ramco Systems, Everstage, Zuper | 9 s | $0.067 |
| Company profile (Word) | Real .docx file, 37 KB | 6 s | about $0.01 |

Findings:
- **ICP search costs more than the page said:** $0.067, not $0.03–0.04. The page and docs now say about $0.05–0.07.
- **Research took 69 s, down from 255 s** before the 25 s per-page limit.
- **Only 1 lead for Freshworks.** The Director-and-above filter is strict, and Hunter returned few senior people for this domain. Use "Find more leads" or Apollo for big companies.
- The test left a real run (Freshworks, with 1 accepted lead) in `leads/`. It is the latest result in the app; nothing was sent to anyone.

## Not tested

- Load from several users at once (not a goal for a one-user app).
