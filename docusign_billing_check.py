#!/usr/bin/env python3
"""
docusign_billing_check.py
-------------------------
Calls the two DocuSign endpoints the Flexera DocuSign connector uses for its
"Envelope consumption overview", so you can see the raw numbers it is fed:

    GET /accounts/{id}/billing_plan      -> plan, term dates   (Allowance)
    GET /accounts/{id}/billing_charges   -> envelopes used      (Used)

If billing_charges says ~273.2K used, Flexera is faithfully showing DocuSign's
own billing counter and the gap is between DocuSign billing and envelope
search. If it says ~165K, the connector is adding something and you have
evidence for a Flexera support case.

Auth and account settings are the same DS_* variables docusign_monthly.py uses
(DS_ACCESS_TOKEN, or DS_INTEGRATION_KEY / DS_CLIENT_SECRET / DS_REFRESH_TOKEN,
plus DS_ACCOUNT_ID). Put this file in the same folder as docusign_monthly.py.

Writes the full responses to docusign_billing_check.json so nothing is lost if
the field names differ from what is printed here.

Usage:
    python docusign_billing_check.py
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv(usecwd=True) or HERE / ".env")
except ImportError:
    pass

try:
    import docusign_monthly as dm   # reuse its auth + retrying GET
except ImportError:
    sys.exit("docusign_monthly.py must sit in the same folder as this script.")


def show_dates(obj, prefix=""):
    """Print every field whose name looks like a date or period, at any depth."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, (dict, list)):
                show_dates(v, path)
            elif any(t in k.lower() for t in ("date", "period", "start", "end", "renew")):
                print(f"    {path}: {v}")
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            show_dates(item, f"{prefix}[{i}]")


def main():
    token = dm.get_access_token()
    base_path, account_id, account_name = dm.get_account_context(token)
    print(f"Account: {account_name} ({account_id})\n")

    plan = dm.api_get(f"{base_path}/billing_plan", token,
                      params={"include_credit_card_information": "false"})
    charges = dm.api_get(f"{base_path}/billing_charges", token,
                         params={"include_charges": "envelopes"})

    out = HERE / "docusign_billing_check.json"
    out.write_text(json.dumps({"billing_plan": plan, "billing_charges": charges}, indent=2),
                   encoding="utf-8")

    bp = plan.get("billingPlan") or {}
    print("BILLING PLAN")
    print(f"    planName: {bp.get('planName', '(not returned)')}")
    print("  date / period fields:")
    show_dates(plan)

    print("\nBILLING CHARGES (envelope lines)")
    items = charges.get("billingChargeItems") or []
    env_items = [c for c in items
                 if "envelope" in json.dumps(c).lower()] or items
    if not env_items:
        print("    (no charge items returned - see the JSON file)")
    for c in env_items:
        keys = ("chargeName", "chargeType", "included", "used", "unitPrice",
                "blocked", "incremental", "firstEffectiveDate", "lastEffectiveDate")
        print("   ", ", ".join(f"{k}={c.get(k)}" for k in keys if k in c) or json.dumps(c))

    print(f"\nFull responses saved to {out.name}")
    print("Compare 'used' with Flexera's Used (273.2K) and 'included' with Allowance (339.8K).")


if __name__ == "__main__":
    main()
