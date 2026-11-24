"""Usage: python cli.py "Razorpay" "Zoho, zoho.com" [--product "..."] [--criteria "..."]"""
import agent.env  # noqa: F401 - loads .env before anything reads the environment

import argparse
import asyncio
import sys

from agent.pipeline import parse_companies, run_agent

sys.stdout.reconfigure(encoding="utf-8")
parser = argparse.ArgumentParser(usage='python cli.py "<company>" ["<company>, domain.com" ...] [--product "..."]')
parser.add_argument("companies", nargs="*")
parser.add_argument("--product")
parser.add_argument("--criteria")
parser.add_argument("--fresh", action="store_true", help="research again even if done in the last 7 days")
args = parser.parse_args()
if not args.companies:
    print('Usage: python cli.py "<company>" ["<company>, domain.com" ...] [--product "..."]', file=sys.stderr)
    sys.exit(1)


def show(e):
    if e["type"] == "stage":
        print(f"\n== {e['message']}")
    elif e["type"] == "log":
        print(f"   {e['message']}")


result = asyncio.run(run_agent({"companies": [c for a in args.companies for c in parse_companies(a)], "product": args.product, "criteria": args.criteria, "fresh": args.fresh}, show))
for c in result["companies"]:
    print(f"\n#{c['rank_position']} {c['name']} ({c['domain']}) — {c['fit_score']}/100 {c['tier']}, willingness to buy: {c['willingness_to_buy']}")
    print(f"   {c['verdict']}")
    print(f"   Pitch: {c['pitch_angle']}")
    for k, f in c["financials"].items():
        if f:
            others = f" | others: {'; '.join(f'{o['value']} ({o['period']}, {o['url']})' for o in f['others'])}" if f["others"] else ""
            print(f"   {k}: {f['value']} ({f['period']}, {f['basis']}, {f['entity']}) — {f['url']}{others}")
        else:
            print(f"   {k}: Unknown")
    for e in [e for e in c["evidence"] if e["status"] == "verified"][:3]:
        print(f'   • "{e["quote"]}" — {e["url"]}')
for f in result["failed"]:
    print(f"\n✗ {f['input_name']}: {f['error']}")
print("\nSaved to leads/leads.json")
