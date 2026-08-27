#!/usr/bin/env python3
"""
zendesk_user_extract.py
=======================

Pulls Name / Email / Role for Zendesk users within a date window and writes CSV.

Why it is built this way
------------------------
1. GET /api/v2/users has NO date filter. The only reliable time-bounded pull is
   the Incremental Export API:
       GET /api/v2/incremental/users/cursor?start_time=<unix>
   It returns every user whose updated_at >= start_time, ordered ascending,
   ~1000 per page, paged with an opaque `after_cursor` until end_of_stream.

2. created_at <= updated_at is always true, so an incremental pull from
   `--start` is guaranteed to contain every user CREATED on or after --start.
   We therefore fetch once and filter locally on whichever date field you pick.

3. The `role` field only ever returns "end-user" / "agent" / "admin". Light
   agents and contributors both come back as role="agent". To separate them you
   need `role_type` and `custom_role_id`:
       role_type  0 = custom agent
                  1 = light agent
                  2 = chat agent
                  3 = contributor (chat agent added to Support, Chat Phase 4)
                  4 = admin
                  5 = billing admin
   `custom_role_id` is resolved against GET /api/v2/custom_roles for the
   human-readable role name (Enterprise plans and above).

Docs
----
Users object + role_type ... https://developer.zendesk.com/api-reference/ticketing/users/users/
Incremental exports ........ https://developer.zendesk.com/documentation/api-basics/working-with-data/using-the-incremental-export-api/
Custom roles ............... https://developer.zendesk.com/api-reference/ticketing/account-configuration/custom_roles/

Setup
-----
    pip install requests

Credentials resolve automatically, in this order of precedence:

    1. Environment variables - ZENDESK_SUBDOMAIN / ZENDESK_EMAIL / ZENDESK_API_TOKEN
       Best for scheduled or CI runs.

    2. A zendesk_config.ini file. Searched in the current directory, then next to
       this script, then ~/.zendesk_config.ini. Format:

           [zendesk]
           subdomain = modeloramamx
           email     = admin@example.com
           api_token = xxxxxxxx

    3. Interactive prompt, if neither of the above is found and you're on a
       terminal. The token input is hidden, and you're offered the chance to
       save it to ~/.zendesk_config.ini for next time.

Get a token from Admin Center > Apps and integrations > Zendesk API. Note the
email must belong to an admin ON THAT ZENDESK INSTANCE - Zendesk basic auth is
"{email}/token:{api_token}", where the email decides whose permissions apply.

Behind a corporate proxy (Zscaler etc.) you may also need:
    export HTTPS_PROXY=http://proxy:8080
    export REQUESTS_CA_BUNDLE=/path/to/corporate-root-ca.pem

Usage
-----
    # everyone created in FY26 Q1
    python zendesk_user_extract.py --start 2026-04-01 --end 2026-06-30

    # trailing window, no dates to type
    python zendesk_user_extract.py --last-days 90 --exclude-end-users

    # only team members (drops end users), by last-modified date
    python zendesk_user_extract.py --start 2026-01-01 --end 2026-06-30 \
        --date-field updated --exclude-end-users

    # only the roles you actually pay for
    python zendesk_user_extract.py --start 2026-01-01 --end 2026-06-30 \
        --exclude-end-users --exclude-seat-class "Light agent,Contributor"
"""

from __future__ import annotations

import argparse
import configparser
import csv
import getpass
import os
import stat
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

API_TIMEOUT = 60
PAGE_PAUSE = 1.5          # incremental export has its own tight rate limit
MAX_RETRIES = 5

# From the Users API reference (role_type is read-only, set by Zendesk)
ROLE_TYPE_LABELS = {
    0: "Custom agent",
    1: "Light agent",
    2: "Chat agent",
    3: "Contributor",
    4: "Admin",
    5: "Billing admin",
}

CSV_COLUMNS = [
    "id",
    "name",
    "email",
    "role",              # raw API value: end-user / agent / admin
    "role_type",         # raw integer
    "seat_class",        # human label derived from role + role_type
    "custom_role_name",  # resolved from /custom_roles
    "active",
    "suspended",
    "created_at",
    "updated_at",
    "last_login_at",
    "organization_id",
]


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #

def build_session(email: str, token: str) -> requests.Session:
    s = requests.Session()
    s.auth = (f"{email}/token", token)
    s.headers.update({"Accept": "application/json"})
    return s


def get_json(session: requests.Session, url: str, params: dict | None = None) -> dict:
    """GET with 429 / 5xx retry, honouring Retry-After."""
    for attempt in range(1, MAX_RETRIES + 1):
        resp = session.get(url, params=params, timeout=API_TIMEOUT)

        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 60))
            print(f"  rate limited, sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue

        if resp.status_code in (500, 502, 503, 504) and attempt < MAX_RETRIES:
            wait = 2 ** attempt
            print(f"  HTTP {resp.status_code}, retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue

        if resp.status_code == 401:
            raise SystemExit(
                "401 Unauthorised. Check ZENDESK_EMAIL / ZENDESK_API_TOKEN, and that "
                "token access is enabled in Admin Center > Apps and integrations > Zendesk API."
            )
        if resp.status_code == 403:
            raise SystemExit(
                "403 Forbidden. The incremental user export endpoint requires an admin account."
            )

        resp.raise_for_status()
        return resp.json()

    raise SystemExit(f"Gave up after {MAX_RETRIES} attempts on {url}")


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def fetch_custom_roles(session: requests.Session, base: str) -> dict[int, str]:
    """id -> name. Returns {} on non-Enterprise plans, which is fine."""
    try:
        data = get_json(session, f"{base}/api/v2/custom_roles")
    except requests.HTTPError:
        print("  custom roles unavailable on this plan, continuing", file=sys.stderr)
        return {}
    roles = {r["id"]: r.get("name", "") for r in data.get("custom_roles", [])}
    print(f"  resolved {len(roles)} custom role(s)", file=sys.stderr)
    return roles


def fetch_users_since(session: requests.Session, base: str, start_epoch: int) -> list[dict]:
    """Cursor-based incremental export from start_epoch to now."""
    url = f"{base}/api/v2/incremental/users/cursor"
    params = {"start_time": start_epoch, "per_page": 1000}
    users: list[dict] = []
    page = 0

    while True:
        page += 1
        data = get_json(session, url, params)
        batch = data.get("users", [])
        users.extend(batch)
        print(f"  page {page}: +{len(batch)} (running total {len(users)})", file=sys.stderr)

        if data.get("end_of_stream"):
            break
        cursor = data.get("after_cursor")
        if not cursor:
            break
        params = {"cursor": cursor}  # start_time is only for the first request
        time.sleep(PAGE_PAUSE)

    return users


# --------------------------------------------------------------------------- #
# Transform
# --------------------------------------------------------------------------- #

def classify(user: dict, custom_roles: dict[int, str]) -> tuple[str, str]:
    """Return (seat_class, custom_role_name)."""
    role = user.get("role") or ""
    role_type = user.get("role_type")
    crid = user.get("custom_role_id")
    custom_name = custom_roles.get(crid, "") if crid else ""

    if role == "end-user":
        return "End user", ""

    if role_type in ROLE_TYPE_LABELS:
        label = ROLE_TYPE_LABELS[role_type]
        # A "custom agent" is only meaningful with its role name attached
        if role_type == 0 and custom_name:
            label = f"Custom agent: {custom_name}"
        return label, custom_name

    if role == "admin":
        return "Admin", custom_name
    if role == "agent":
        return "Agent", custom_name
    return role or "Unknown", custom_name


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def to_row(user: dict, custom_roles: dict[int, str]) -> dict:
    seat_class, custom_name = classify(user, custom_roles)
    return {
        "id": user.get("id"),
        "name": user.get("name") or "",
        "email": user.get("email") or "",
        "role": user.get("role") or "",
        "role_type": user.get("role_type") if user.get("role_type") is not None else "",
        "seat_class": seat_class,
        "custom_role_name": custom_name,
        "active": user.get("active"),
        "suspended": user.get("suspended"),
        "created_at": user.get("created_at") or "",
        "updated_at": user.get("updated_at") or "",
        "last_login_at": user.get("last_login_at") or "",
        "organization_id": user.get("organization_id") or "",
    }


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #

CONFIG_NAME = "zendesk_config.ini"


def sanitize_subdomain(raw: str) -> str:
    """
    Accept whatever people actually type: a bare subdomain, the full
    https://x.zendesk.com/ URL, with or without a trailing slash, with or
    without http(s)://. Always return just the short subdomain, e.g.
    "modeloramamx".
    """
    value = raw.strip()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.strip("/")
    value = value.split("/", 1)[0]           # drop any path after the host
    if value.endswith(".zendesk.com"):
        value = value[: -len(".zendesk.com")]
    return value.strip()

# Searched in order. First file that exists wins.
def config_candidates(explicit: str | None) -> list[Path]:
    if explicit:
        return [Path(explicit).expanduser()]
    return [
        Path.cwd() / CONFIG_NAME,
        Path(__file__).resolve().parent / CONFIG_NAME,
        Path.home() / f".{CONFIG_NAME}",
    ]


def read_config_file(explicit: str | None) -> tuple[dict, Path | None]:
    for path in config_candidates(explicit):
        if path.is_file():
            parser = configparser.ConfigParser()
            parser.read(path, encoding="utf-8")
            if parser.has_section("zendesk"):
                print(f"  loaded config from {path}", file=sys.stderr)
                return dict(parser["zendesk"]), path
    return {}, None


def save_config_file(path: Path, subdomain: str, email: str, token: str) -> None:
    parser = configparser.ConfigParser()
    parser["zendesk"] = {"subdomain": subdomain, "email": email, "api_token": token}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        parser.write(fh)
    try:  # POSIX only; silently skipped on Windows
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except (OSError, NotImplementedError):
        pass
    print(f"  saved to {path} (keep this out of git)", file=sys.stderr)


def load_config(explicit: str | None, allow_prompt: bool = True) -> tuple[str, str, str]:
    """
    Resolve subdomain / email / token from, in order of precedence:
        1. environment variables  (best for CI and scheduled runs)
        2. zendesk_config.ini     (best for repeated manual runs)
        3. interactive prompt     (first run; offers to save)
    """
    file_cfg, _ = read_config_file(explicit)

    subdomain = os.environ.get("ZENDESK_SUBDOMAIN") or file_cfg.get("subdomain", "")
    email = os.environ.get("ZENDESK_EMAIL") or file_cfg.get("email", "")
    token = os.environ.get("ZENDESK_API_TOKEN") or file_cfg.get("api_token", "")

    missing = [n for n, v in (("subdomain", subdomain), ("email", email), ("token", token)) if not v]
    if not missing:
        return sanitize_subdomain(subdomain), email, token

    if not allow_prompt or not sys.stdin.isatty():
        raise SystemExit(
            f"Missing {', '.join(missing)}. Set ZENDESK_SUBDOMAIN / ZENDESK_EMAIL / "
            f"ZENDESK_API_TOKEN, or create {CONFIG_NAME}:\n\n"
            "  [zendesk]\n  subdomain = modeloramamx\n  email = admin@example.com\n  api_token = xxxx\n"
        )

    print(f"\nFirst run - need {', '.join(missing)}.\n", file=sys.stderr)
    if not subdomain:
        subdomain = input("Zendesk subdomain (the bit before .zendesk.com): ").strip()
    if not email:
        email = input("Zendesk account email (must be an admin on that instance): ").strip()
    if not token:
        token = getpass.getpass("API token (input hidden): ").strip()

    if input("\nSave these for next time? [y/N]: ").strip().lower() == "y":
        save_config_file(Path.home() / f".{CONFIG_NAME}", subdomain, email, token)

    return sanitize_subdomain(subdomain), email, token


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_window(start: str, end: str | None) -> tuple[datetime, datetime]:
    start_dt = datetime.fromisoformat(start)
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    if end:
        end_dt = datetime.fromisoformat(end)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        if len(end) == 10:  # bare YYYY-MM-DD means include the whole day
            end_dt += timedelta(days=1) - timedelta(seconds=1)
    else:
        end_dt = datetime.now(timezone.utc)
    if end_dt <= start_dt:
        raise SystemExit("--end must be after --start")
    return start_dt, end_dt


def main() -> None:
    p = argparse.ArgumentParser(description="Export Zendesk users (name, email, role) for a date window.")
    p.add_argument("--start", help="Window start, YYYY-MM-DD or ISO datetime (UTC)")
    p.add_argument("--end", help="Window end, YYYY-MM-DD or ISO datetime (UTC). Default: now")
    p.add_argument("--last-days", type=int,
                   help="Shorthand for a trailing window, e.g. --last-days 90. Alternative to --start")
    p.add_argument("--config", help=f"Path to a {CONFIG_NAME} file. Default: auto-discover")
    p.add_argument("--no-prompt", action="store_true",
                   help="Never prompt for credentials; fail instead. Use for scheduled runs")
    p.add_argument("--date-field", choices=["created", "updated"], default="created",
                   help="Which timestamp the window applies to (default: created)")
    p.add_argument("--out", default="zendesk_users.csv", help="Output CSV path")
    p.add_argument("--exclude-end-users", action="store_true",
                   help="Drop role=end-user, leaving team members only")
    p.add_argument("--exclude-seat-class", default="",
                   help='Comma-separated seat_class values to drop, e.g. "Light agent,Contributor"')
    p.add_argument("--active-only", action="store_true", help="Drop deleted/inactive users")
    args = p.parse_args()

    if not args.start and not args.last_days:
        raise SystemExit("Give either --start YYYY-MM-DD or --last-days N")
    if args.last_days:
        start_dt = datetime.now(timezone.utc) - timedelta(days=args.last_days)
        end_dt = datetime.now(timezone.utc)
    else:
        start_dt, end_dt = parse_window(args.start, args.end)

    print("Resolving credentials...", file=sys.stderr)
    subdomain, email, token = load_config(args.config, allow_prompt=not args.no_prompt)
    if not subdomain or "." in subdomain or " " in subdomain:
        raise SystemExit(
            f"Subdomain looks wrong: {subdomain!r}. It should be just the short name, "
            f"e.g. 'modeloramamx' for modeloramamx.zendesk.com - not the full URL.\n"
            f"If this came from a saved config file, fix or delete it:\n"
            f"  Remove-Item \"$HOME\\.{CONFIG_NAME}\""
        )
    base = f"https://{subdomain}.zendesk.com"
    print(f"  target: {base}", file=sys.stderr)
    session = build_session(email, token)

    print(f"Window: {start_dt:%Y-%m-%d %H:%M} to {end_dt:%Y-%m-%d %H:%M} UTC "
          f"on {args.date_field}_at", file=sys.stderr)

    print("Fetching custom roles...", file=sys.stderr)
    custom_roles = fetch_custom_roles(session, base)

    print("Fetching users (incremental export)...", file=sys.stderr)
    raw_users = fetch_users_since(session, base, int(start_dt.timestamp()))

    # Deduplicate: time-based/cursor exports can repeat a record across pages
    seen: dict[int, dict] = {}
    for u in raw_users:
        seen[u["id"]] = u

    field = f"{args.date_field}_at"
    drop_classes = {c.strip() for c in args.exclude_seat_class.split(",") if c.strip()}

    rows = []
    for u in seen.values():
        ts = parse_ts(u.get(field))
        if ts is None or not (start_dt <= ts <= end_dt):
            continue
        if args.exclude_end_users and u.get("role") == "end-user":
            continue
        if args.active_only and u.get("active") is False:
            continue
        row = to_row(u, custom_roles)
        if row["seat_class"] in drop_classes:
            continue
        rows.append(row)

    rows.sort(key=lambda r: (r["seat_class"], r["name"].lower()))

    with open(args.out, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nFetched {len(seen)} unique users, {len(rows)} in window -> {args.out}", file=sys.stderr)
    print("\nBreakdown by seat_class:", file=sys.stderr)
    for cls, n in Counter(r["seat_class"] for r in rows).most_common():
        print(f"  {cls:<32} {n:>6}", file=sys.stderr)


if __name__ == "__main__":
    main()
