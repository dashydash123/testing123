#!/usr/bin/env python3
"""
zendesk_flexera.py
------------------
ZENDESK API -> FLEXERA (Snow Atlas) SaaS DATA IMPORT, in one run.

  1. Pull every agent / admin from the Zendesk Users API (full roster)
  2. Build the two Flexera CSVs in memory-safe text form (IDs never
     touch Excel, so no scientific-notation problem)
  3. Push each file: token -> request upload URL -> PUT to blob -> import

Flexera mappings this script writes for
  user assignments (8fc88134...)          user activities (e5f01062...)
    Email address        = email            Unique user identifier = id
    Unique user id       = id               Activity or event name = last_login_at
    Subscription name    = custom_role_name Activity recorded value = last_login_at
    Display name         = name
    Username             = name
    Account creation     = created_at
    Account last updated = last_login_at

Users who have never logged in, or who have no email (e.g. the "AI Agent"
bot), are still sent. Flexera may warn on those rows and skip only them.
Suspended users are sent as-is.

Roles listed in ZENDESK_EXCLUDE_ROLES are left out of every file. Each name is
matched (case-insensitive) against custom_role_name OR seat_class, so both
"Agente Light" and "Light agent" work.

.env (next to this script):
    ZENDESK_DOMAIN=yoursubdomain
    ZENDESK_EMAIL=you@company.com
    ZENDESK_API_TOKEN=...
    FLEXERA_CLIENT_ID=...
    FLEXERA_CLIENT_SECRET=...
    ZENDESK_EXCLUDE_ROLES=Agente Light          (optional, comma-separated)

Usage:
    python zendesk_flexera.py            # pull from Zendesk and push to Flexera
    python zendesk_flexera.py --dry-run  # pull and write the CSVs, push nothing
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time

import requests
from dotenv import find_dotenv, load_dotenv

# --------------------------- CONFIGURATION ---------------------------

FLEXERA_BASE = "https://snowatlas-westeurope.flexera.eu"
TOKEN_URL    = f"{FLEXERA_BASE}/idp/api/connect/token"
UPLOAD_URL   = f"{FLEXERA_BASE}/api/saas/import/v1/uploads"

ASSIGNMENTS_CONFIG_ID = "8fc88134-1797-4f0f-bdfc-1e83ad00d384"   # user assignments
ACTIVITIES_CONFIG_ID  = "e5f01062-75ac-43fe-94cc-b79ac0b250ba"   # user activities

OUTPUT_DIR      = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_CSV    = os.path.join(OUTPUT_DIR, "zendesk_users_snapshot.csv")  # for review only
ASSIGNMENTS_CSV = os.path.join(OUTPUT_DIR, "zendesk_license.csv")
ACTIVITIES_CSV  = os.path.join(OUTPUT_DIR, "zendesk_activities.csv")

# Output headers - must match the CSV fields in each Flexera mapping EXACTLY
ASSIGNMENTS_TEMPLATE = ["email", "id", "custom_role_name", "name",
                        "created_at", "last_login_at"]
ACTIVITIES_TEMPLATE  = ["id", "last_login_at"]
SNAPSHOT_TEMPLATE    = ["id", "name", "email", "role", "role_type", "seat_class",
                        "custom_role_name", "active", "suspended",
                        "created_at", "updated_at", "last_login_at"]

ONLY_ACTIVE_USERS                 = True   # drop deleted users (active == False)
DROP_BLANK_ACTIVITY_ROWS          = False  # False = send never-logged-in users too (Flexera warns)
FILL_SUBSCRIPTION_FROM_SEAT_CLASS = True   # blank custom_role_name -> seat_class

API_TIMEOUT = 60
MAX_RETRIES = 5

ROLE_TYPE_LABELS = {
    0: "Custom agent", 1: "Light agent", 2: "Chat agent",
    3: "Contributor", 4: "Admin", 5: "Billing admin",
}


# ------------------------------ SETTINGS -----------------------------

def sanitize_domain(raw: str) -> str:
    value = raw.strip()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.strip("/").split("/")[0]
    if value.endswith(".zendesk.com"):
        value = value[:-len(".zendesk.com")]
    return value.strip()

def load_settings() -> dict[str, str]:
    dotenv_path = find_dotenv(usecwd=True) or os.path.join(OUTPUT_DIR, ".env")
    load_dotenv(dotenv_path=dotenv_path)
    names = ["ZENDESK_DOMAIN", "ZENDESK_EMAIL", "ZENDESK_API_TOKEN",
             "FLEXERA_CLIENT_ID", "FLEXERA_CLIENT_SECRET"]
    cfg = {n: os.getenv(n, "").strip() for n in names}
    missing = [n for n, v in cfg.items() if not v]
    if missing:
        sys.exit(f"Missing in .env ({dotenv_path}): {', '.join(missing)}")
    cfg["ZENDESK_EXCLUDE_ROLES"] = os.getenv("ZENDESK_EXCLUDE_ROLES", "").strip().strip('"').strip("'")
    cfg["ZENDESK_DOMAIN"] = sanitize_domain(cfg["ZENDESK_DOMAIN"])
    if not cfg["ZENDESK_DOMAIN"] or "." in cfg["ZENDESK_DOMAIN"]:
        sys.exit("ZENDESK_DOMAIN is invalid. Use 'yourcompany' or "
                 "'https://yourcompany.zendesk.com'.")
    return cfg


# ------------------------------ ZENDESK ------------------------------

def zd_get(session: requests.Session, url: str, params: dict | None = None) -> dict:
    """GET with 429 / 5xx retry, honouring Retry-After."""
    for attempt in range(1, MAX_RETRIES + 1):
        resp = session.get(url, params=params, timeout=API_TIMEOUT)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 60))
            print(f"  Zendesk rate limit, sleeping {wait}s")
            time.sleep(wait)
            continue
        if resp.status_code in (500, 502, 503, 504) and attempt < MAX_RETRIES:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 401:
            sys.exit("Zendesk 401 Unauthorised. Check ZENDESK_EMAIL / ZENDESK_API_TOKEN "
                     "and that token access is enabled in Admin Center.")
        if resp.status_code == 403:
            sys.exit("Zendesk 403 Forbidden. The account needs admin rights.")
        resp.raise_for_status()
        return resp.json()
    sys.exit(f"Zendesk: gave up after {MAX_RETRIES} attempts on {url}")

def fetch_custom_roles(session: requests.Session, base: str) -> dict[int, str]:
    try:
        data = zd_get(session, f"{base}/api/v2/custom_roles")
    except requests.HTTPError:
        print("  custom roles unavailable on this plan, continuing")
        return {}
    return {r["id"]: r.get("name", "") for r in data.get("custom_roles", [])}

def fetch_agents(session: requests.Session, base: str) -> list[dict]:
    """Every agent and admin (end users filtered out by Zendesk)."""
    url: str | None = f"{base}/api/v2/users"
    params: dict | None = {"page[size]": 100, "role[]": ["agent", "admin"]}
    users: list[dict] = []
    page = 0
    while url:
        page += 1
        data = zd_get(session, url, params)
        batch = data.get("users", [])
        users.extend(batch)
        print(f"  page {page}: +{len(batch)} (total {len(users)})")
        if not (data.get("meta") or {}).get("has_more"):
            break
        url = (data.get("links") or {}).get("next")
        params = None
        time.sleep(0.3)
    return users

def seat_class(user: dict, custom_roles: dict[int, str]) -> tuple[str, str]:
    """Return (seat_class, custom_role_name)."""
    role = user.get("role") or ""
    role_type = user.get("role_type")
    crid = user.get("custom_role_id")
    custom_name = custom_roles.get(crid, "") if crid else ""
    if role == "end-user":
        return "End user", ""
    if role_type in ROLE_TYPE_LABELS:
        label = ROLE_TYPE_LABELS[role_type]
        if role_type == 0 and custom_name:
            label = f"Custom agent: {custom_name}"
        return label, custom_name
    return ("Admin" if role == "admin" else "Agent" if role == "agent" else role or "Unknown"), custom_name

def to_row(user: dict, custom_roles: dict[int, str]) -> dict[str, str]:
    cls, custom_name = seat_class(user, custom_roles)
    return {
        "id": str(user.get("id") or ""),          # text, exact digits
        "name": (user.get("name") or "").strip(),
        "email": (user.get("email") or "").strip().lower(),
        "role": user.get("role") or "",
        "role_type": "" if user.get("role_type") is None else str(user["role_type"]),
        "seat_class": cls,
        "custom_role_name": custom_name,
        "active": str(user.get("active")).upper(),
        "suspended": str(user.get("suspended")).upper(),
        "created_at": user.get("created_at") or "",
        "updated_at": user.get("updated_at") or "",
        "last_login_at": user.get("last_login_at") or "",
    }


# ------------------------------ FLEXERA ------------------------------

def get_access_token(client_id: str, client_secret: str) -> str:
    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials",
              "client_id": client_id, "client_secret": client_secret},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    print("Obtained Flexera access token.")
    return resp.json()["access_token"]

def request_upload_url(token: str) -> tuple[str, str]:
    resp = requests.post(UPLOAD_URL, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    print(f"  upload URL received (fileId={data.get('fileId')}, expires in {data.get('expires')}s)")
    return data["url"], data["fileId"]

def upload_csv_to_blob(presigned_url: str, csv_path: str) -> None:
    with open(csv_path, "rb") as f:
        resp = requests.put(presigned_url, data=f,
                            headers={"x-ms-blob-type": "BlockBlob", "Content-Type": "text/csv"},
                            timeout=120)
    resp.raise_for_status()
    print(f"  uploaded to blob storage (HTTP {resp.status_code})")

def trigger_import(token: str, file_id: str, config_id: str) -> None:
    url = f"{FLEXERA_BASE}/api/saas/import/v1/configs/{config_id}/records/import"
    resp = requests.post(url,
                         headers={"Authorization": f"Bearer {token}",
                                  "Content-Type": "application/json"},
                         data=json.dumps({"fileId": file_id}), timeout=60)
    resp.raise_for_status()
    print(f"  import task created (HTTP {resp.status_code}): {resp.text[:500]}")

def push_to_flexera(token: str, csv_path: str, config_id: str, label: str) -> None:
    print(f"\n--- Pushing {label} ({os.path.basename(csv_path)}) ---")
    url, file_id = request_upload_url(token)
    upload_csv_to_blob(url, csv_path)
    trigger_import(token, file_id, config_id)


# ------------------------------ PIPELINE -----------------------------

def write_csv(path: str, rows: list[dict], columns: list[str], bom: bool = False) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig" if bom else "utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

def main() -> None:
    ap = argparse.ArgumentParser(description="Zendesk users -> Flexera SaaS import")
    ap.add_argument("--dry-run", action="store_true",
                    help="Pull from Zendesk and write the CSVs, but push nothing to Flexera")
    args = ap.parse_args()

    cfg = load_settings()
    base = f"https://{cfg['ZENDESK_DOMAIN']}.zendesk.com"
    session = requests.Session()
    session.auth = (f"{cfg['ZENDESK_EMAIL']}/token", cfg["ZENDESK_API_TOKEN"])
    session.headers.update({"Accept": "application/json"})

    print(f"Zendesk: {base}")
    custom_roles = fetch_custom_roles(session, base)
    print(f"  {len(custom_roles)} custom role(s)")
    raw = fetch_agents(session, base)

    by_id: dict[str, dict] = {}
    for u in raw:
        by_id[str(u.get("id"))] = u        # de-duplicate
    rows = [to_row(u, custom_roles) for u in by_id.values()]

    if ONLY_ACTIVE_USERS:
        rows = [r for r in rows if r["active"] != "FALSE"]
    rows = [r for r in rows if r["id"]]

    # Roles to leave out entirely (ZENDESK_EXCLUDE_ROLES in .env)
    exclude = {x.strip().lower() for x in cfg["ZENDESK_EXCLUDE_ROLES"].split(",") if x.strip()}
    excluded: dict[str, int] = {}
    if exclude:
        kept = []
        for r in rows:
            hit = next((v for v in (r["custom_role_name"], r["seat_class"])
                        if v and v.strip().lower() in exclude), None)
            if hit:
                excluded[hit] = excluded.get(hit, 0) + 1
            else:
                kept.append(r)
        rows = kept
        unmatched = exclude - {k.lower() for k in excluded}
        for name in sorted(unmatched):
            print(f"  NOTE: excluded role '{name}' matched no user - check the spelling")
    no_email = sum(1 for r in rows if not r["email"])

    filled = 0
    if FILL_SUBSCRIPTION_FROM_SEAT_CLASS:
        for r in rows:
            if not r["custom_role_name"]:
                r["custom_role_name"] = r["seat_class"]
                filled += 1

    rows.sort(key=lambda r: (r["seat_class"], r["name"].lower()))
    never = sum(1 for r in rows if not r["last_login_at"])
    activities = [r for r in rows if r["last_login_at"]] if DROP_BLANK_ACTIVITY_ROWS else rows

    write_csv(SNAPSHOT_CSV, rows, SNAPSHOT_TEMPLATE, bom=True)
    write_csv(ASSIGNMENTS_CSV, rows, ASSIGNMENTS_TEMPLATE)
    write_csv(ACTIVITIES_CSV, activities, ACTIVITIES_TEMPLATE)

    print(f"\nWrote {len(rows)} assignment rows -> {ASSIGNMENTS_CSV}")
    for name, n in sorted(excluded.items()):
        print(f"  excluded by ZENDESK_EXCLUDE_ROLES: {n} x {name}")
    if no_email:
        print(f"  {no_email} user(s) have no email (e.g. bots) - sent anyway, "
              f"expect Flexera warning(s)")
    if filled:
        print(f"  {filled} blank custom_role_name filled from seat_class")
    note = (f"{never} never-logged-in left out" if DROP_BLANK_ACTIVITY_ROWS
            else f"{never} never logged in - expect {never} Flexera warning(s)")
    print(f"Wrote {len(activities)} activity rows   -> {ACTIVITIES_CSV}  ({note})")
    print(f"Snapshot for review                -> {SNAPSHOT_CSV}")

    if args.dry_run:
        print("\n--dry-run: nothing sent to Flexera.")
        return

    token = get_access_token(cfg["FLEXERA_CLIENT_ID"], cfg["FLEXERA_CLIENT_SECRET"])
    if rows:
        push_to_flexera(token, ASSIGNMENTS_CSV, ASSIGNMENTS_CONFIG_ID, "user assignments")
    if activities:
        push_to_flexera(token, ACTIVITIES_CSV, ACTIVITIES_CONFIG_ID, "user activities")
    print("\nDone. Check Flexera > Data imports for the two tasks.")

if __name__ == "__main__":
    main()
