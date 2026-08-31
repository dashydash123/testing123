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

Auth: two ways, pick either.

  A) Paste an access token you already have (nothing is rotated):
       set DS_ACCESS_TOKEN=<token>

  B) Let the script mint one from your refresh token:
       set DS_INTEGRATION_KEY=<the 'username' from the Postman/Bruno yml>
       set DS_CLIENT_SECRET=<the 'password' from that yml>
       set DS_REFRESH_TOKEN=<the refresh_token value from that yml>

     WARNING: option B rotates the refresh token. DocuSign issues a new one
     and the old may stop working. If other integrations share this refresh
     token, coordinate first. The script prints the new value when it changes.

  If DS_ACCESS_TOKEN is set, option A wins and no refresh happens.

Requirements
------------
    pip install requests

Also set
--------
  set DS_ACCOUNT_ID=<API account ID>
  set DS_FROM_DATE=2025-01-01
  set DS_TO_DATE=2025-12-31

Note: this reads ALL users and ALL envelopes on the account, which requires an
account admin. If the person who authorised isn't an admin, you'll see only
their own data -- the script warns you if that looks to be the case.

Outputs (CSV + JSON) into ./docusign_export/
"""

import base64
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

ACCESS_TOKEN = os.getenv("DS_ACCESS_TOKEN", "")        # option A
INTEGRATION_KEY = os.getenv("DS_INTEGRATION_KEY", "")  # option B: yml 'username'
CLIENT_SECRET = os.getenv("DS_CLIENT_SECRET", "")      # option B: yml 'password'
REFRESH_TOKEN = os.getenv("DS_REFRESH_TOKEN", "")      # option B
ACCOUNT_ID = os.getenv("DS_ACCOUNT_ID", "")            # API Account ID

# Date range for envelope extraction (inclusive). Envelope search REQUIRES from_date.
FROM_DATE = os.getenv("DS_FROM_DATE", "2025-01-01")
TO_DATE = os.getenv("DS_TO_DATE", datetime.now(timezone.utc).strftime("%Y-%m-%d"))

# "created"  -> envelopes CREATED in the window (use this for licence/usage counting)
# "changed"  -> envelopes whose status CHANGED in the window (DocuSign default)
FROM_TO_STATUS = os.getenv("DS_FROM_TO_STATUS", "created")

# Split the range into windows so no single call returns an unbounded result set.
WINDOW_DAYS = int(os.getenv("DS_WINDOW_DAYS", "30"))

OUTPUT_DIR = Path(os.getenv("DS_OUTPUT_DIR", "docusign_export"))

USERS_PAGE_SIZE = int(os.getenv("DS_USERS_PAGE_SIZE", "50"))  # v2.1 max is 100
ENVELOPES_PAGE_SIZE = 100  # conservative; listStatusChanges caps at 1000 per call

# additional_info=true adds lastLogin and login status, but makes DocuSign do
# extra per-user lookups -- on big accounts that's what causes read timeouts.
# Off by default. Set DS_USER_DETAIL=1 to turn it back on if you need lastLogin.
USER_ADDITIONAL_INFO = os.getenv("DS_USER_DETAIL", "") == "1"

# Seconds to wait for a single API response before giving up and retrying.
HTTP_TIMEOUT = int(os.getenv("DS_TIMEOUT", "180"))

AUTH_HOST = "account-d.docusign.com" if ENVIRONMENT == "demo" else "account.docusign.com"

# ---------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------


def get_access_token():
    """
    Option A: use DS_ACCESS_TOKEN as-is. Nothing is rotated, nothing is
    invalidated -- safe when others share these credentials.

    Option B: if no access token was given, mint one from the refresh token.
    This DOES rotate the refresh token, so it only happens when you've
    deliberately left DS_ACCESS_TOKEN unset.
    """
    if ACCESS_TOKEN:
        return ACCESS_TOKEN

    if not (INTEGRATION_KEY and CLIENT_SECRET and REFRESH_TOKEN):
        sys.exit(
            "No credentials. Either:\n"
            "  set DS_ACCESS_TOKEN=<token>\n"
            "or, to generate one:\n"
            "  set DS_INTEGRATION_KEY=<yml username>\n"
            "  set DS_CLIENT_SECRET=<yml password>\n"
            "  set DS_REFRESH_TOKEN=<yml refresh_token>"
        )

    print("No DS_ACCESS_TOKEN set -- generating one from the refresh token.")
    print("(This rotates the refresh token.)\n")

    # Basic auth: integration key as username, secret key as password --
    # exactly what 'auth: type: basic' does in the Bruno/Postman yml.
    basic = base64.b64encode(f"{INTEGRATION_KEY}:{CLIENT_SECRET}".encode()).decode()

    resp = requests.post(
        f"https://{AUTH_HOST}/oauth/token",
        headers={"Authorization": f"Basic {basic}"},
        data={"grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN},
        timeout=30,
    )

    if resp.status_code != 200:
        sys.exit(
            f"Token request failed ({resp.status_code}). Common causes: the "
            "refresh token was already rotated by someone else, it has expired "
            "(~30 days), or the integration key / secret don't match the app "
            f"that issued it.\n\nDocuSign said: {resp.text}"
        )

    data = resp.json()
    new_refresh = data.get("refresh_token")

    # Tokens are long and wrap badly in cmd, so also drop them in a file you
    # can open in Notepad and copy cleanly.
    try:
        lines = [f'set DS_ACCESS_TOKEN={data["access_token"]}']
        if new_refresh and new_refresh != REFRESH_TOKEN:
            lines.append("")
            lines.append(f"set DS_REFRESH_TOKEN={new_refresh}")
        Path("docusign_tokens.txt").write_text("\n".join(lines), encoding="utf-8")
        wrote_file = True
    except OSError:
        wrote_file = False

    if new_refresh and new_refresh != REFRESH_TOKEN:
        print("=" * 70)
        print("NEW REFRESH TOKEN -- save this, the old one may now be dead:\n")
        print(new_refresh)
        print("\nTell anyone else sharing these credentials.")
        print("=" * 70 + "\n")

    print("=" * 70)
    print("ACCESS TOKEN (valid ~8 hours). To re-run without rotating the")
    print("refresh token again, use:\n")
    print(f'set DS_ACCESS_TOKEN={data["access_token"]}')
    print("=" * 70)
    if wrote_file:
        print("\nAlso saved to docusign_tokens.txt -- easier to copy from there.")
        print("Delete that file when you're done with it.\n")

    return data["access_token"]


def get_account_context(token):
    """Resolve base_uri and account_id from the userinfo endpoint."""
    resp = requests.get(
        f"https://{AUTH_HOST}/oauth/userinfo",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )

    if resp.status_code == 401:
        sys.exit(
            "Token rejected (401). If you supplied DS_ACCESS_TOKEN, it has most "
            "likely expired -- these last about 8 hours. Either paste a fresh "
            "one, or unset DS_ACCESS_TOKEN and let the script generate one from "
            "the refresh token.\n\n"
            f"If the token is definitely fresh, check DS_ENV: it's '{ENVIRONMENT}', "
            f"so the token is going to {AUTH_HOST}. A demo token sent to "
            "production fails the same way."
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
        try:
            resp = requests.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                params=params,
                timeout=HTTP_TIMEOUT,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt == max_retries - 1:
                sys.exit(
                    f"Request kept timing out after {max_retries} attempts:\n"
                    f"  {url}\n\n"
                    "The account may be large enough that DocuSign is slow to "
                    "respond. Things to try:\n"
                    "  set DS_TIMEOUT=300           (wait longer per request)\n"
                    "  set DS_USERS_PAGE_SIZE=25    (smaller pages)\n"
                    "  set DS_WINDOW_DAYS=7         (smaller date windows)\n\n"
                    f"Underlying error: {e}"
                )
            wait = 5 * (attempt + 1)
            print(f"  timed out, retrying in {wait}s ...", flush=True)
            time.sleep(wait)
            continue

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

        if 400 <= resp.status_code < 500:
            # DocuSign puts a useful errorCode/message in the body -- show it
            # rather than just the status code.
            try:
                err = resp.json()
                detail = f"{err.get('errorCode', '')}: {err.get('message', resp.text)}"
            except ValueError:
                detail = resp.text
            sys.exit(
                f"DocuSign rejected the request ({resp.status_code}).\n\n"
                f"  {detail}\n\n"
                f"URL: {resp.url}"
            )

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
        params = {
            "start_position": start,
            "count": USERS_PAGE_SIZE,
        }
        if USER_ADDITIONAL_INFO:
            params["additional_info"] = "true"

        data = api_get(f"{base_path}/users", token, params=params)

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
