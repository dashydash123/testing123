#!/usr/bin/env python3
"""
zendesk_to_flexera.py
---------------------
ZENDESK -> FLEXERA (Snow Atlas) SaaS DATA IMPORT

Reads the CSV written by Zendesk_User_Extrsct_Env.py (zendesk_users.csv),
builds one file per Flexera import configuration, and pushes each through:
    token -> request upload URL -> PUT CSV to blob -> trigger import

Each output file carries only the columns its Flexera mapping reads, with
the same header names as zendesk_users.csv:

  assignments (config 8fc88134...)
      Email address of the user   = email
      Unique user identifier      = id
      Subscription name           = custom_role_name  (blank -> seat_class)
      Display name of the user    = name
      Username of the user        = name
      User account creation       = created_at
      User account last updated   = last_login_at     (blank if never logged in)

  activities (config e5f01062...)
      Unique user identifier      = id
      Activity or event name      = last_login_at
      Activity recorded value     = last_login_at

Users who have never logged in are sent in BOTH files. Flexera flags their
activity row with a warning ("Activity name / recorded value must be
specified") and skips that row only; the rest of the import goes through.
Set DROP_BLANK_ACTIVITY_ROWS = True to leave them out and avoid the warnings.

.env (same file the Zendesk extract uses):
    FLEXERA_CLIENT_ID=...
    FLEXERA_CLIENT_SECRET=...
"""

from __future__ import annotations

import json
import os
import sys

import pandas as pd
import requests
from dotenv import find_dotenv, load_dotenv

# --------------------------- CONFIGURATION ---------------------------

BASE       = "https://snowatlas-westeurope.flexera.eu"
TOKEN_URL  = f"{BASE}/idp/api/connect/token"
UPLOAD_URL = f"{BASE}/api/saas/import/v1/uploads"

# Import configuration IDs (Flexera > Zendesk > API Integration panel)
ASSIGNMENTS_CONFIG_ID = "8fc88134-1797-4f0f-bdfc-1e83ad00d384"   # user assignments
ACTIVITIES_CONFIG_ID  = "e5f01062-75ac-43fe-94cc-b79ac0b250ba"   # user activities

# Output of Zendesk_User_Extrsct_Env.py. Do NOT open and re-save this in
# Excel: it rewrites the long Zendesk IDs as 1.91524E+12 and they no
# longer match the users in Flexera.
ZENDESK_SOURCE_FILE = "zendesk_users.csv"

OUTPUT_DIR      = os.getcwd()
ASSIGNMENTS_CSV = os.path.join(OUTPUT_DIR, "zendesk_license.csv")
ACTIVITIES_CSV  = os.path.join(OUTPUT_DIR, "zendesk_activities.csv")

ID_COLUMN           = "id"
EMAIL_COLUMN        = "email"
SUBSCRIPTION_COLUMN = "custom_role_name"
LAST_LOGIN_COLUMN   = "last_login_at"

# Output headers - must match the CSV fields in each Flexera mapping EXACTLY
ASSIGNMENTS_TEMPLATE = ["email", "id", "custom_role_name", "name",
                        "created_at", "last_login_at"]
ACTIVITIES_TEMPLATE  = ["id", "last_login_at"]

# Every column the activity mapping reads (Activity name, Activity value).
ACTIVITY_REQUIRED_COLUMNS = [LAST_LOGIN_COLUMN]

# False = send every user in the activities file; never-logged-in users
# produce a Flexera warning for their row (accepted). True = leave them out.
DROP_BLANK_ACTIVITY_ROWS = False

# Subscription name is the licence. A user with no custom role (e.g. a Light
# agent, or a plan without custom roles) would otherwise land in Flexera with
# no subscription, so fill the gap from seat_class ("Light agent", "Admin"...).
FILL_SUBSCRIPTION_FROM_SEAT_CLASS = True

EXCLUDE_END_USERS = True    # drop role == end-user (no paid seat)
ONLY_ACTIVE_USERS = True    # drop deleted users (active == False)
PUSH_TO_FLEXERA   = True    # False = build the CSVs only, no API calls


# --------------------------- LOAD & VALIDATE -------------------------

def _blank(series: pd.Series) -> pd.Series:
    s = series.fillna("").astype(str).str.strip()
    return s.eq("") | s.str.lower().isin(["nan", "nat", "none", "null"])

def load_zendesk_users() -> pd.DataFrame:
    if not os.path.exists(ZENDESK_SOURCE_FILE):
        sys.exit(f"Zendesk source file not found: {ZENDESK_SOURCE_FILE}")
    # dtype=str keeps the 13-14 digit IDs exactly as written.
    # utf-8-sig strips the BOM the extract script adds for Excel.
    df = pd.read_csv(ZENDESK_SOURCE_FILE, dtype=str, encoding="utf-8-sig",
                     keep_default_na=False)
    print(f"Loaded {len(df)} rows from {ZENDESK_SOURCE_FILE}.")

    needed = sorted(set(ASSIGNMENTS_TEMPLATE) | set(ACTIVITIES_TEMPLATE))
    missing = [c for c in needed if c not in df.columns]
    if missing:
        sys.exit(f"{ZENDESK_SOURCE_FILE} is missing column(s): {missing}. "
                 f"Found: {list(df.columns)}")

    ids = df[ID_COLUMN].str.strip()
    mangled = ids[ids.str.contains(r"[eE]\+|\.", regex=True)]
    if len(mangled):
        sys.exit(
            f"{len(mangled)} ID(s) look like Excel scientific notation "
            f"(e.g. '{mangled.iloc[0]}'). The file was re-saved in Excel and the "
            f"IDs are no longer exact. Re-run Zendesk_User_Extrsct_Env.py and use "
            f"its CSV without opening/saving it in Excel."
        )
    return df


# ------------------------- FLEXERA API -------------------------------

def get_access_token(client_id: str, client_secret: str) -> str:
    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials",
              "client_id": client_id, "client_secret": client_secret},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    print("Obtained access token.")
    return resp.json()["access_token"]

def request_upload_url(token: str) -> tuple[str, str]:
    resp = requests.post(UPLOAD_URL,
                         headers={"Authorization": f"Bearer {token}"}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    print(f"Received upload URL (fileId={data.get('fileId')}, "
          f"expires in {data.get('expires')}s).")
    return data["url"], data["fileId"]

def upload_csv_to_blob(presigned_url: str, csv_path: str) -> None:
    with open(csv_path, "rb") as f:
        resp = requests.put(
            presigned_url, data=f,
            headers={"x-ms-blob-type": "BlockBlob", "Content-Type": "text/csv"},
            timeout=120,
        )
    resp.raise_for_status()
    print(f"Uploaded CSV to blob storage (HTTP {resp.status_code}).")

def trigger_import(token: str, file_id: str, config_id: str) -> None:
    url = f"{BASE}/api/saas/import/v1/configs/{config_id}/records/import"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        data=json.dumps({"fileId": file_id}),
        timeout=60,
    )
    resp.raise_for_status()
    print(f"Import task created (HTTP {resp.status_code}). Response: {resp.text[:500]}")

def push_to_flexera(token: str, csv_path: str, config_id: str, label: str) -> None:
    print(f"\n--- Pushing {label} ({os.path.basename(csv_path)}) ---")
    presigned_url, file_id = request_upload_url(token)
    upload_csv_to_blob(presigned_url, csv_path)
    trigger_import(token, file_id, config_id=config_id)


# ------------------------------ PIPELINE -----------------------------

def main() -> None:
    load_dotenv(find_dotenv(usecwd=True) or None)
    df = load_zendesk_users()

    df[ID_COLUMN] = df[ID_COLUMN].str.strip()
    df[EMAIL_COLUMN] = df[EMAIL_COLUMN].str.strip().str.lower()

    if EXCLUDE_END_USERS and "role" in df.columns:
        df = df[df["role"].str.strip().str.lower() != "end-user"]
    if ONLY_ACTIVE_USERS and "active" in df.columns:
        df = df[df["active"].str.strip().str.lower() != "false"]

    # Clean text-like blanks ("nan", "None") out of every column we send
    for col in set(ASSIGNMENTS_TEMPLATE) | set(ACTIVITIES_TEMPLATE):
        df[col] = df[col].where(~_blank(df[col]), "").str.strip()

    # ---- User assignments: every user with an id and email ----
    no_key = _blank(df[ID_COLUMN]) | _blank(df[EMAIL_COLUMN])
    users = (df[~no_key]
             .drop_duplicates(subset=[ID_COLUMN])
             .reset_index(drop=True))

    no_sub = _blank(users[SUBSCRIPTION_COLUMN])
    filled = 0
    if FILL_SUBSCRIPTION_FROM_SEAT_CLASS and "seat_class" in users.columns:
        filled = int((no_sub & ~_blank(users["seat_class"])).sum())
        users.loc[no_sub, SUBSCRIPTION_COLUMN] = users.loc[no_sub, "seat_class"]
    still_no_sub = int(_blank(users[SUBSCRIPTION_COLUMN]).sum())

    assignments = users[ASSIGNMENTS_TEMPLATE]

    # ---- User activities: every user (blank last login -> Flexera warning) ----
    no_activity = pd.Series(False, index=users.index)
    for col in ACTIVITY_REQUIRED_COLUMNS:
        no_activity |= _blank(users[col])
    keep = ~no_activity if DROP_BLANK_ACTIVITY_ROWS else pd.Series(True, index=users.index)
    activities = users.loc[keep, ACTIVITIES_TEMPLATE].reset_index(drop=True)

    # Plain UTF-8 (no BOM), comma-delimited, IDs written as text
    assignments.to_csv(ASSIGNMENTS_CSV, index=False, encoding="utf-8")
    activities.to_csv(ACTIVITIES_CSV, index=False, encoding="utf-8")

    print(f"Wrote {len(assignments)} assignment rows -> {ASSIGNMENTS_CSV}"
          f"  ({int(no_key.sum())} dropped: blank id/email)")
    print(f"   headers: {list(assignments.columns)}")
    if filled:
        print(f"   {filled} blank custom_role_name filled from seat_class")
    if still_no_sub:
        print(f"   WARNING: {still_no_sub} user(s) still have no subscription name")
    blank_n = int(no_activity.sum())
    if DROP_BLANK_ACTIVITY_ROWS:
        note = f"{blank_n} left out: blank {', '.join(ACTIVITY_REQUIRED_COLUMNS)}"
    else:
        note = f"{blank_n} with blank {', '.join(ACTIVITY_REQUIRED_COLUMNS)} - expect {blank_n} Flexera warning(s)"
    print(f"Wrote {len(activities)} activity rows   -> {ACTIVITIES_CSV}  ({note})")
    print(f"   headers: {list(activities.columns)}")

    if not PUSH_TO_FLEXERA:
        print("PUSH_TO_FLEXERA is False - CSVs built, nothing sent.")
        return

    client_id = os.getenv("FLEXERA_CLIENT_ID", "").strip()
    client_secret = os.getenv("FLEXERA_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        sys.exit("FLEXERA_CLIENT_ID / FLEXERA_CLIENT_SECRET are not set in .env.")

    token = get_access_token(client_id, client_secret)
    if len(assignments):
        push_to_flexera(token, ASSIGNMENTS_CSV, ASSIGNMENTS_CONFIG_ID, "user assignments")
    if len(activities):
        push_to_flexera(token, ACTIVITIES_CSV, ACTIVITIES_CONFIG_ID, "user activities")

if __name__ == "__main__":
    main()
