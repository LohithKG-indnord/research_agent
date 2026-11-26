"""GDPR duties for leads (personal data): tell people where their details came from (Article 14), let them be erased and
never contacted again (Articles 17 and 21), and keep their details no longer than needed (Article 5(1)(e)).

- privacy_notice / with_notice: the line every email and LinkedIn follow-up ends with, added in code so it is never left out.
- forget: deletes a person from every saved run, the database and the raw provider logs, and remembers them only as a
  one-way hash so a later lookup can't bring them back.
- sweep: deletes leads older than RETENTION_DAYS (never contacted: since they were found; contacted: since the contact)."""
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import db_file, open_db
from .util import to_json

MARK = "\n\n-- \n"  # the standard signature separator: everything after it is the notice
RESULT_FILE = re.compile(r"^([0-9a-f-]{36}|leads)\.json$")
DEFAULT_RETENTION_DAYS = 365


def retention_days():
    """RETENTION_DAYS from .env (at least 30), else 365."""
    try:
        return max(30, int(os.environ.get("RETENTION_DAYS") or DEFAULT_RETENTION_DAYS))
    except ValueError:
        return DEFAULT_RETENTION_DAYS


def privacy_notice(person):
    source = {"hunter": "Hunter.io", "apollo": "Apollo.io"}.get(person.get("source"), "a business contact database")
    link = os.environ.get("PRIVACY_POLICY_URL", "").strip() or "[add your privacy policy link: set PRIVACY_POLICY_URL in .env]"
    return (f"{MARK}I found your work contact details through {source}. If you'd rather not hear from me, reply \"no\" "
            f"and I won't contact you again. How we handle your data: {link}")


def with_notice(text, person):
    """`text` with exactly one, current privacy notice at the end."""
    return str(text or "").split(MARK)[0].rstrip() + privacy_notice(person)


def without_notice(message):
    return {k: (v.split(MARK)[0].rstrip() if k in ("email", "followup") and isinstance(v, str) else v) for k, v in (message or {}).items()}


# ---------- do-not-contact list ----------
def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _keys(person, domain):
    """One-way hashes that identify a person without storing who they are."""
    raw = [f"email:{person['email'].strip().lower()}" if person.get("email") else None,
           f"name:{_norm(person.get('name'))}|{str(domain or '').lower()}" if person.get("name") and domain else None]
    return {hashlib.sha256(k.encode()).hexdigest() for k in raw if k}


def _suppress_file(directory):
    return Path(directory) / "suppressed.json"


def _suppressed(directory):
    try:
        return set(json.loads(_suppress_file(directory).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def is_suppressed(person, domain, directory):
    return bool(_keys(person, domain) & _suppressed(directory))


# ---------- removing people ----------
def _same(a, b, domain_a, domain_b):
    if a.get("email") and b.get("email"):
        return a["email"].strip().lower() == b["email"].strip().lower()
    if a.get("profile_url") and a.get("profile_url") == b.get("profile_url"):
        return True
    return bool(a.get("name")) and _norm(a["name"]) == _norm(b.get("name")) and bool(domain_a) and domain_a == domain_b


def _result_files(directory):
    return [f for f in Path(directory).iterdir() if RESULT_FILE.match(f.name)] if Path(directory).is_dir() else []


def _remove_from_db(directory, people, older_than=None):
    """Delete these people (and, via cascade, their links and sources) plus raw provider payloads that name them."""
    try:
        db = open_db(db_file(directory))
    except (OSError, sqlite3.Error):
        return
    with db:
        for p in people:
            db.execute("DELETE FROM people WHERE (? <> '' AND lower(email) = ?) OR (? <> '' AND linkedin_url = ?) OR (? <> '' AND provider_id = ?)",
                       ((p.get("email") or "").lower(),) * 2 + ((p.get("profile_url") or ""),) * 2 + ((p.get("id") or ""),) * 2)
            for needle in [x for x in ((p.get("email") or "").lower(), p.get("name") or "") if x]:
                db.execute("DELETE FROM provider_responses WHERE provider_run_id IN (SELECT id FROM provider_runs WHERE provider IN ('hunter', 'apollo')) AND instr(lower(payload), lower(?)) > 0", (needle,))
        if older_than:  # contact-database replies hold many people: none is kept past the retention period
            db.execute("DELETE FROM provider_responses WHERE retrieved_at < ? AND provider_run_id IN (SELECT id FROM provider_runs WHERE provider IN ('hunter', 'apollo'))", (older_than,))


def _rewrite(directory, keep):
    """Apply keep(person, company) to every saved run; returns the people removed."""
    removed = []
    for f in _result_files(directory):
        try:
            result = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        changed = False
        for c in (result or {}).get("companies") or []:
            people = c.get("people") or []
            kept = [p for p in people if keep(p, c)]
            if len(kept) != len(people):
                gone = [p for p in people if p not in kept]
                removed += gone
                c["people"] = kept
                for p in gone:  # their buying-role classification is personal data too
                    ((c.get("buying_committee") or {}).get("people") or {}).pop(p.get("id"), None)
                changed = True
        if changed:
            f.write_text(to_json(result, indent=2), encoding="utf-8")
    return removed


def forget_person(directory, run_id, company_id, person_id):
    """Erase one person everywhere and add them to the do-not-contact list. Returns how many copies were removed."""
    try:
        result = json.loads((Path(directory) / f"{run_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Unknown run.") from None
    company = next((c for c in result.get("companies") or [] if c.get("id") == company_id), None)
    person = next((p for p in (company or {}).get("people") or [] if p.get("id") == person_id), None)
    if not person:
        raise ValueError("Unknown lead.")
    domain = company.get("domain")
    hashes = _suppressed(directory) | _keys(person, domain)
    _suppress_file(directory).write_text(json.dumps(sorted(hashes)), encoding="utf-8")
    removed = _rewrite(directory, lambda p, c: not _same(p, person, c.get("domain"), domain))
    _remove_from_db(directory, [person, *removed])
    return len(removed)


def _parse(at):
    try:
        return datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return None


def sweep(directory, days=None, now=None):
    """Delete leads past the retention period and add the privacy notice to messages written before it existed.
    Returns how many people were removed."""
    days = days or retention_days()
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)

    def keep(p, _c):
        sent = (p.get("outreach") or {}).get("status") == "sent"
        at = _parse((p.get("outreach") or {}).get("at") if sent else p.get("checked_at"))
        return not at or at >= cutoff
    removed = _rewrite(directory, keep)
    _remove_from_db(directory, removed, older_than=cutoff.isoformat(timespec="milliseconds").replace("+00:00", "Z"))
    # Messages written before notices existed get one now, so nothing goes out without it.
    for f in _result_files(directory):
        try:
            result = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        changed = False
        for c in (result or {}).get("companies") or []:
            for p in c.get("people") or []:
                m = p.get("message")
                if m and MARK not in str(m.get("email") or ""):
                    m["email"] = with_notice(m.get("email"), p)
                    if m.get("followup"):
                        m["followup"] = with_notice(m["followup"], p)
                    changed = True
        if changed:
            f.write_text(to_json(result, indent=2), encoding="utf-8")
    return len(removed)
