"""Buying capacity: a rule-of-thumb estimate of what a company could spend on a new offering, from its verified revenue,
and how ready it looks to buy (growth, profit, funding, willingness). No model call: the arithmetic is shown to the user."""
import re

# Cross-industry rule of thumb: technology budgets run at about 3-7% of revenue, and roughly 10-20% of that goes to new
# initiatives rather than keeping existing systems running.
TECH_SHARE = (0.03, 0.07)
NEW_SHARE = (0.10, 0.20)

UNITS = [  # longest first, so "lakh crore" wins over "crore"
    (r"lakh\s*crore|lac\s*crore", 1e12), (r"crore|\bcr\b", 1e7), (r"lakhs?|\blacs?\b", 1e5),
    (r"trillion|\btn\b", 1e12), (r"billion|\bbn\b|\bb\b", 1e9), (r"million|\bmn\b|\bm\b|\bmm\b", 1e6), (r"thousand|\bk\b", 1e3),
]
CURRENCIES = [(r"₹|\brs\b|\binr\b|rupee", "INR"), (r"\$|\busd\b|dollar", "USD"), (r"€|\beur\b|euro", "EUR"), (r"£|\bgbp\b|pound", "GBP")]
SYMBOL = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}


def parse_money(value):
    """"₹17.66 Cr" -> (176600000.0, "INR"); "$7.6 million" -> (7600000.0, "USD"); None if it isn't an amount."""
    text = str(value or "").lower().replace(",", "")
    number = re.search(r"\d+(?:\.\d+)?", text)
    currency = next((code for pattern, code in CURRENCIES if re.search(pattern, text)), None)
    if not number or not currency:
        return None
    rest = text[number.end():]
    scale = next((mult for pattern, mult in UNITS if re.search(rf"^\s*(?:{pattern})", rest)), 1)
    return float(number.group()) * scale, currency


def money(amount, currency):
    """An amount the way people read it: crore/lakh for rupees, K/M/B otherwise."""
    sign = SYMBOL[currency]
    if currency == "INR":
        if amount >= 1e7:
            return f"{sign}{amount / 1e7:,.1f} Cr".replace(".0 ", " ")
        return f"{sign}{amount / 1e5:,.1f} lakh".replace(".0 ", " ")
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if amount >= div:
            return f"{sign}{amount / div:,.1f}{unit}".replace(".0", "")
    return f"{sign}{amount:,.0f}"


def _percent(value):
    m = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    return float(m.group()) if m else None


def buying_capacity(financials, willingness):
    """{"affinity": high|medium|low, "reasons": [...], "tech_budget", "new_offering_budget", "basis"} (budgets None without revenue)."""
    fin = financials or {}
    signals = []  # (points, reason)
    growth = _percent((fin.get("growth") or {}).get("value"))
    if growth is not None and growth >= 15:
        signals.append((1, f"Growing fast ({growth:+.0f}% revenue)"))
    elif growth is not None and growth < 0:
        signals.append((-1, f"Revenue shrinking ({growth:+.0f}%)"))
    profit = (fin.get("profit") or {}).get("value")
    if profit and re.search(r"loss|-\s*[₹$€£]?\s*\d|\(\s*[₹$€£]?\s*\d", str(profit), re.I):
        signals.append((-1, f"Loss-making ({profit})"))
    elif profit:
        signals.append((1, f"Profitable ({profit})"))
    if fin.get("funding"):
        signals.append((1, f"Has raised funding ({fin['funding']['value']})"))
    signals.append(({"high": 2, "medium": 1}.get(willingness, 0), f"Willingness to buy: {willingness or 'unknown'}"))
    points, reasons = sum(p for p, _ in signals), [r for _, r in signals]
    out = {"affinity": "high" if points >= 3 else "medium" if points >= 1 else "low", "reasons": reasons,
           "tech_budget": None, "new_offering_budget": None, "basis": None}
    rev = fin.get("revenue") or {}
    parsed = parse_money(rev.get("value"))
    if parsed:
        amount, cur = parsed
        lo, hi = amount * TECH_SHARE[0], amount * TECH_SHARE[1]
        out["tech_budget"] = f"{money(lo, cur)} – {money(hi, cur)} a year"
        out["new_offering_budget"] = f"{money(lo * NEW_SHARE[0], cur)} – {money(hi * NEW_SHARE[1], cur)} a year"
        out["basis"] = (f"Revenue {rev['value']} ({rev.get('period') or 'period unknown'}): technology budgets typically run at "
                        f"{TECH_SHARE[0]:.0%}–{TECH_SHARE[1]:.0%} of revenue, and {NEW_SHARE[0]:.0%}–{NEW_SHARE[1]:.0%} of that goes to new offerings.")
    return out
