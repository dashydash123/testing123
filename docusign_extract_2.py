#!/usr/bin/env python3
"""
DocuSign eSignature REST API extractor
======================================

Pulls:
  1. All account users (name, email, user ID, status, permission profile, last login)
  2. All envelopes in a date range, with recipients expanded
  3. A per-user rollup: envelopes SENT by the user and envelopes the user was a
     RECIPIENT on -- kept as separate columns, because these are different numbers
     and conflating them is the usual cause of "envelope count doesn't match".

Auth: uses an existing access token as-is (the kind Postman's OAuth 2.0 flow
      produces). Read-only -- never calls the refresh endpoint, so it cannot
      rotate or invalidate any credential another integration is using.

Requirements
------------
    pip install requests

Setup
-----
  set DS_ACCESS_TOKEN=<access token from Postman>
  set DS_ACCOUNT_ID=<API account ID>
  set DS_FROM_DATE=2025-01-01
  set DS_TO_DATE=2025-12-31

Access tokens from this flow last ~8 hours. When yours expires, have Postman
issue a new one and update DS_ACCESS_TOKEN.

Note: this script reads ALL users and ALL envelopes on the account. That
requires the token to belong to an account admin. If the person who authorised
in Postman is not an admin, you'll see only their own data.

Outputs (CSV + JSON) into ./docusign_export/
"""

import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

ENVIRONMENT = os.getenv("DS_ENV", "production")  # "demo" or "production"

ACCESS_TOKEN = os.getenv("DS_ACCESS_TOKEN", "")  # access token from Postman
ACCOUNT_ID = os.getenv("DS_ACCOUNT_ID", "")      # API Account ID

# Date range for envelope extraction (inclusive). Envelope search REQUIRES from_date.
FROM_DATE = os.getenv("DS_FROM_DATE", "2025-01-01")
TO_DATE = os.getenv("DS_TO_DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))

# "created"  -> envelopes CREATED in the window (use this for licence/usage counting)
# "changed"  -> envelopes whose status CHANGED in the window (DocuSign default)
FROM_TO_STATUS = os.getenv("DS_FROM_TO_STATUS", "created")

# Split the range into windows so no single call returns an unbounded result set.
WINDOW_DAYS = int(os.getenv("DS_WINDOW_DAYS", "30"))

OUTPUT_DIR = Path(os.getenv("DS_OUTPUT_DIR", "docusign_export"))

USERS_PAGE_SIZE = 100      # v2.1 hard max for Users:list
ENVELOPES_PAGE_SIZE = 100  # conservative; listStatusChanges caps at 1000 per call

AUTH_HOST = "account-d.docusign.com" if ENVIRONMENT == "demo" else "account.docusign.com"

# ---------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------


def get_access_token():
    """
    Use the supplied access token as-is.

    No refresh call is ever made, so this script cannot rotate or invalidate
    the token pair anyone else is relying on. Using a bearer token to read
    does not consume it.
    """
    return ACCESS_TOKEN


def get_account_context(token):
    """Resolve base_uri and account_id from the userinfo endpoint."""
    resp = requests.get(
        f"https://{AUTH_HOST}/oauth/userinfo",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )

    if resp.status_code == 401:
        sys.exit(
            "Access token rejected (401). Most likely it has expired -- these "
            "last about 8 hours. Have Postman issue a new one and update "
            "DS_ACCESS_TOKEN.\n\n"
            f"If the token is definitely fresh, check DS_ENV: it's '{ENVIRONMENT}', "
            f"so the token is being sent to {AUTH_HOST}. A demo-environment token "
            "sent to production fails the same way."
        )

    resp.raise_for_status()
    accounts = resp.json()["accounts"]

    if ACCOUNT_ID:
        account = next((a for a in accounts if a["account_id"] == ACCOUNT_ID), None)
        if account is None:
            sys.exit(f"Account {ACCOUNT_ID} not visible to this user.")
    else:
        account = next((a for a in accounts if a.get("is_default")), accounts[0])

    # base_uri looks like https://na3.docusign.net -- append the REST path yourself
    base_path = f"{account['base_uri']}/restapi/v2.1/accounts/{account['account_id']}"
    return base_path, account["account_id"], account.get("account_name", "")


# ---------------------------------------------------------------------------
# HTTP HELPER WITH RATE-LIMIT HANDLING
# ---------------------------------------------------------------------------

# DocuSign default: 3,000 calls/hour/account plus a 500-call/30-second burst cap.
# Both return HTTP 429. X-RateLimit-Reset is a UTC epoch timestamp.


def api_get(url, token, params=None, max_retries=5):
    for attempt in range(max_retries):
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params=params,
            timeout=120,
        )

        if resp.status_code == 429:
            reset = resp.headers.get("X-RateLimit-Reset")
            if reset:
                wait = max(int(reset) - int(time.time()), 1)
            else:
                wait = 2 ** attempt * 30
            print(f"  rate limited, sleeping {wait}s ...", flush=True)
            time.sleep(min(wait, 3700))
            continue

        if resp.status_code >= 500:
            time.sleep(2 ** attempt)
            continue

        resp.raise_for_status()

        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining is not None and int(remaining) < 50:
            print(f"  warning: only {remaining} API calls left this hour", flush=True)

        return resp.json()

    raise RuntimeError(f"Giving up on {url} after {max_retries} attempts")


# ---------------------------------------------------------------------------
# USERS
# ---------------------------------------------------------------------------


def fetch_users(base_path, token):
    """GET /users -- paginated. additional_info=true adds login status and profile."""
    users, start = [], 0
    while True:
        try:
            data = api_get(
                f"{base_path}/users",
                token,
                params={
                    "start_position": start,
                    "count": USERS_PAGE_SIZE,
                    "additional_info": "true",
                    "status": "Active,Created,Closed",
                },
            )
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 403:
                sys.exit(
                    "Permission denied listing users (403).\n\n"
                    "The account that authorised this token isn't an account "
                    "admin, so it can't see the full user list. Whoever set this "
                    "up in Postman needs to re-authorise as an admin user, or "
                    "have admin rights granted."
                )
            raise

        batch = data.get("users", [])
        users.extend(batch)

        total = int(data.get("totalSetSize", len(users)))
        start += len(batch)
        print(f"  users {start}/{total}", flush=True)
        if not batch or start >= total:
            break

    if len(users) == 1:
        print(
            "\n  WARNING: only 1 user returned. This usually means the token "
            "belongs to a non-admin user who can only see themselves.\n",
            flush=True,
        )

    return users


# ---------------------------------------------------------------------------
# ENVELOPES
# ---------------------------------------------------------------------------


def date_windows(from_date, to_date, days):
    start = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    while start < end:
        stop = min(start + timedelta(days=days), end)
        yield start, stop
        start = stop


def fetch_envelopes(base_path, token):
    """
    GET /envelopes (Envelopes:listStatusChanges).

    from_date is mandatory. include=recipients returns the full recipient block so
    you can attribute an envelope to signers/CCs, not just the sender.
    """
    envelopes = []
    for w_start, w_end in date_windows(FROM_DATE, TO_DATE, WINDOW_DAYS):
        start_pos = 0
        while True:
            data = api_get(
                f"{base_path}/envelopes",
                token,
                params={
                    "from_date": w_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "to_date": w_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "from_to_status": FROM_TO_STATUS,
                    "include": "recipients,custom_fields",
                    "count": ENVELOPES_PAGE_SIZE,
                    "start_position": start_pos,
                    "order_by": "created",
                    "order": "asc",
                },
            )
            batch = data.get("envelopes", [])
            envelopes.extend(batch)
            if not batch:
                break

            start_pos += len(batch)
            total = int(data.get("totalSetSize", 0))
            print(
                f"  {w_start:%Y-%m-%d} to {w_end:%Y-%m-%d}: {start_pos}/{total}",
                flush=True,
            )
            if total and start_pos >= total:
                break

    return envelopes


RECIPIENT_TYPES = [
    "signers",
    "carbonCopies",
    "certifiedDeliveries",
    "agents",
    "editors",
    "inPersonSigners",
    "intermediaries",
    "witnesses",
    "notaries",
]


def iter_recipients(envelope):
    recips = envelope.get("recipients") or {}
    for rtype in RECIPIENT_TYPES:
        for r in recips.get(rtype, []) or []:
            yield rtype, r


# ---------------------------------------------------------------------------
# ROLLUP
# ---------------------------------------------------------------------------


def build_summary(users, envelopes):
    """
    Per-user envelope attribution.

    sent_*      : envelopes where the user is envelope.sender
    recipient_* : envelopes where the user appears in envelope.recipients

    Matching is done on userId first, then falls back to lowercased email --
    external recipients have no userId, and internal recipients sometimes don't
    either if they were added by email rather than picked from the address book.
    """
    by_id, by_email = {}, {}
    summary = {}

    for u in users:
        uid = u.get("userId")
        email = (u.get("email") or "").lower()
        row = {
            "userId": uid,
            "userName": u.get("userName", ""),
            "email": u.get("email", ""),
            "userStatus": u.get("userStatus", ""),
            "userType": u.get("userType", ""),
            "isAdmin": u.get("isAdmin", ""),
            "permissionProfileName": u.get("permissionProfileName", ""),
            "createdDateTime": u.get("createdDateTime", ""),
            "lastLogin": u.get("lastLogin", ""),
            "sent_envelopes": 0,
            "sent_completed": 0,
            "sent_voided": 0,
            "sent_last_date": "",
            "recipient_envelopes": 0,
            "recipient_signed": 0,
        }
        summary[uid] = row
        by_id[uid] = uid
        if email:
            by_email[email] = uid

    unmatched_senders = {}

    for env in envelopes:
        sender = env.get("sender") or {}
        sid = sender.get("userId")
        semail = (sender.get("email") or "").lower()
        key = sid if sid in summary else by_email.get(semail)

        if key:
            row = summary[key]
            row["sent_envelopes"] += 1
            status = (env.get("status") or "").lower()
            if status == "completed":
                row["sent_completed"] += 1
            elif status in ("voided", "declined"):
                row["sent_voided"] += 1
            created = env.get("createdDateTime", "")
            if created > row["sent_last_date"]:
                row["sent_last_date"] = created
        elif semail or sid:
            # Sender no longer in the users list (closed/deleted account).
            k = semail or sid
            unmatched_senders[k] = unmatched_senders.get(k, 0) + 1

        seen = set()
        for _rtype, r in iter_recipients(env):
            rid = r.get("userId")
            remail = (r.get("email") or "").lower()
            rkey = rid if rid in summary else by_email.get(remail)
            if not rkey or rkey in seen:
                continue
            seen.add(rkey)
            summary[rkey]["recipient_envelopes"] += 1
            if (r.get("status") or "").lower() in ("completed", "signed"):
                summary[rkey]["recipient_signed"] += 1

    return list(summary.values()), unmatched_senders


# ---------------------------------------------------------------------------
# OUTPUT
# ---------------------------------------------------------------------------


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"  wrote {path}  ({len(rows)} rows)")


def flatten_envelopes(envelopes):
    rows = []
    for env in envelopes:
        sender = env.get("sender") or {}
        recips = list(iter_recipients(env))
        rows.append(
            {
                "envelopeId": env.get("envelopeId", ""),
                "status": env.get("status", ""),
                "emailSubject": env.get("emailSubject", ""),
                "createdDateTime": env.get("createdDateTime", ""),
                "sentDateTime": env.get("sentDateTime", ""),
                "completedDateTime": env.get("completedDateTime", ""),
                "lastModifiedDateTime": env.get("lastModifiedDateTime", ""),
                "senderUserId": sender.get("userId", ""),
                "senderName": sender.get("userName", ""),
                "senderEmail": sender.get("email", ""),
                "recipientCount": len(recips),
                "recipientEmails": ";".join(
                    sorted({(r.get("email") or "") for _t, r in recips})
                ),
                "purgeState": env.get("purgeState", ""),
            }
        )
    return rows


def main():
    missing = [n for n, v in [("DS_ACCESS_TOKEN", ACCESS_TOKEN)] if not v]
    if missing:
        sys.exit(f"Missing config: {', '.join(missing)}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Authenticating ...")
    token = get_access_token()
    base_path, account_id, account_name = get_account_context(token)
    print(f"  account: {account_name} ({account_id})")
    print(f"  base:    {base_path}")

    print("Fetching users ...")
    users = fetch_users(base_path, token)

    print(f"Fetching envelopes {FROM_DATE} -> {TO_DATE} (from_to_status={FROM_TO_STATUS}) ...")
    envelopes = fetch_envelopes(base_path, token)

    print("Building per-user rollup ...")
    summary, unmatched = build_summary(users, envelopes)

    write_csv(
        OUTPUT_DIR / "docusign_users.csv",
        [
            {
                "userId": u.get("userId", ""),
                "userName": u.get("userName", ""),
                "email": u.get("email", ""),
                "userStatus": u.get("userStatus", ""),
                "userType": u.get("userType", ""),
                "isAdmin": u.get("isAdmin", ""),
                "permissionProfileName": u.get("permissionProfileName", ""),
                "createdDateTime": u.get("createdDateTime", ""),
                "lastLogin": u.get("lastLogin", ""),
            }
            for u in users
        ],
        [
            "userId", "userName", "email", "userStatus", "userType", "isAdmin",
            "permissionProfileName", "createdDateTime", "lastLogin",
        ],
    )

    env_rows = flatten_envelopes(envelopes)
    write_csv(
        OUTPUT_DIR / "docusign_envelopes.csv",
        env_rows,
        list(env_rows[0].keys()) if env_rows else ["envelopeId"],
    )

    write_csv(
        OUTPUT_DIR / "docusign_user_envelope_summary.csv",
        summary,
        [
            "userId", "userName", "email", "userStatus", "userType", "isAdmin",
            "permissionProfileName", "createdDateTime", "lastLogin",
            "sent_envelopes", "sent_completed", "sent_voided", "sent_last_date",
            "recipient_envelopes", "recipient_signed",
        ],
    )

    with open(OUTPUT_DIR / "docusign_raw.json", "w", encoding="utf-8") as fh:
        json.dump({"users": users, "envelopes": envelopes}, fh, indent=2)

    print(f"\nDone. {len(users)} users, {len(envelopes)} envelopes.")
    if unmatched:
        print(
            f"NOTE: {len(unmatched)} sender(s) sent envelopes but are not in the "
            "current users list (closed or deleted accounts):"
        )
        for k, v in sorted(unmatched.items(), key=lambda x: -x[1])[:20]:
            print(f"  {k}: {v} envelopes")


if __name__ == "__main__":
    main()
