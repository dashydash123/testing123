"""
Flexera One SaaS Management (Snow Atlas) - Subscriptions per user  (v2)
-----------------------------------------------------------------------
Rebuilds "SaaS > Reports > Subscriptions per user" from the subscription
side, so row counts line up with the report (one row per user-subscription).

How it works
  1. Pull all applications; keep those whose vendor/name CONTAINS your text.
  2. Collect subscription IDs from those applications (app details ->
     licenses) plus a sample of each app's users (-> /users/{id}/subscriptions).
  3. For every subscription ID: GET /subscriptions/{id}/users (1000 per page)
     = every user ASSIGNED that subscription.
  4. Look up subscription name / vendor / discovery type, apply filters, write CSV.

Standard library only - runs on embeddable Python.
    python snow_subscriptions_per_user_v2.py
"""

import csv
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# =============================== CONFIG ===============================
CLIENT_ID = "PASTE_CLIENT_ID_HERE"
CLIENT_SECRET = "PASTE_CLIENT_SECRET_HERE"
REGION = "westeurope"

# Filters - "contains", case-insensitive. Leave "" to skip.
VENDOR_CONTAINS = "Microsoft"
APPLICATION_CONTAINS = ""          # e.g. "Microsoft 365"
SUBSCRIPTION_CONTAINS = ""         # e.g. "E3", "Copilot"

# Discovery type of the subscription. [] = all.
#   0 SaaS connector | 1 Manually added | 2 Browser unverified | 3 SSO
#   4 Device         | 7 Browser verified | 8 CASB
DISCOVERY_TYPES = []

SEED_USERS_PER_APP = 200           # users sampled per app to discover subscription IDs
MAX_WORKERS = 8
# ======================================================================

BASE_URL = f"https://{REGION}.snowsoftware.io"
CV = "api/saas/consolidated-view/v1"
PAGE_SIZE = 1000
DISCOVERY_TYPE_NAMES = {
    0: "SaaS connector", 1: "Manually added", 2: "Browser unverified",
    3: "SSO", 4: "Device", 7: "Browser verified", 8: "CASB",
}


class NotFound(Exception):
    pass


class SnowClient:
    def __init__(self):
        self.token = None
        self.lock = threading.Lock()
        self.refresh_token()

    def refresh_token(self, stale=None):
        with self.lock:
            if stale is not None and self.token != stale:
                return
            body = urllib.parse.urlencode({
                "grant_type": "client_credentials",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            }).encode()
            req = urllib.request.Request(
                f"{BASE_URL}/idp/api/connect/token", data=body, method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                self.token = json.load(resp)["access_token"]

    def get(self, path, params=None, retries=6):
        url = f"{BASE_URL}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in range(1, retries + 1):
            token = self.token
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {token}", "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    self.refresh_token(stale=token)
                    continue
                if e.code == 404:
                    raise NotFound(url)
                if e.code in (429, 500, 502, 503, 504):
                    try:
                        wait = int(e.headers.get("Retry-After", ""))
                    except ValueError:
                        wait = min(2 ** attempt, 60)
                    time.sleep(wait)
                    continue
                detail = e.read().decode(errors="ignore")[:300]
                raise RuntimeError(f"HTTP {e.code}: {detail}") from e
            except urllib.error.URLError:
                time.sleep(min(2 ** attempt, 60))
        raise RuntimeError(f"Failed after {retries} attempts: {url}")

    def get_all(self, path, params=None, max_items=None):
        params = dict(params or {})
        items, page = [], 1
        while True:
            params.update(page_size=PAGE_SIZE, page_number=page)
            data = self.get(path, params)
            batch = data.get("items") or []
            items.extend(batch)
            if max_items and len(items) >= max_items:
                return items[:max_items]
            total_pages = (data.get("pagination") or {}).get("total_pages")
            if total_pages is not None:
                if page >= total_pages:
                    break
            elif len(batch) < PAGE_SIZE:
                break
            page += 1
        return items


def contains(value, needle):
    return not needle or needle.lower() in str(value or "").lower()


def sub_matches(s):
    return (contains(s.get("vendor"), VENDOR_CONTAINS)
            and contains(s.get("applicationName"), APPLICATION_CONTAINS)
            and contains(s.get("name"), SUBSCRIPTION_CONTAINS)
            and (not DISCOVERY_TYPES or s.get("discoveryType") in DISCOVERY_TYPES))


def main():
    if "PASTE_" in CLIENT_ID or "PASTE_" in CLIENT_SECRET:
        sys.exit("Fill in CLIENT_ID and CLIENT_SECRET in the CONFIG section.")

    client = SnowClient()
    print(f"Authenticated against {BASE_URL}")

    # 1. Matching applications
    apps = client.get_all(f"{CV}/applications")
    matched = [a for a in apps
               if contains(a.get("vendor"), VENDOR_CONTAINS)
               and contains(a.get("name"), APPLICATION_CONTAINS)]
    print(f"Applications: {len(apps)} | matching: {len(matched)}")
    if not matched:
        sys.exit("No applications match the vendor/application filter.")

    # 2. Discover subscription IDs (+ metadata where available)
    sub_ids = set()
    sub_meta = {}  # subscription id -> subscription record from /users/{id}/subscriptions

    def seed_app(app):
        found, meta = set(), {}
        try:
            detail = client.get(f"{CV}/applications/{app['id']}")
            for lic in detail.get("licenses") or app.get("licenses") or []:
                if lic.get("id"):
                    found.add(lic["id"])
        except (NotFound, RuntimeError):
            pass
        try:
            sample = client.get_all(f"{CV}/applications/{app['id']}/users",
                                    max_items=SEED_USERS_PER_APP)
        except (NotFound, RuntimeError):
            sample = []
        for u in sample:
            try:
                for s in client.get_all(f"{CV}/users/{u['id']}/subscriptions"):
                    meta[s["id"]] = s
                    found.add(s["id"])
            except (NotFound, RuntimeError):
                continue
        return found, meta

    print("Discovering subscription IDs ...")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = [pool.submit(seed_app, a) for a in matched]
        for n, f in enumerate(as_completed(futs), 1):
            found, meta = f.result()
            sub_ids |= found
            sub_meta.update(meta)
            if n % 10 == 0 or n == len(matched):
                print(f"  {n}/{len(matched)} apps scanned | subscription IDs: {len(sub_ids)}")

    # Drop subscriptions we already know don't match the filters
    candidate_ids = [sid for sid in sub_ids
                     if sid not in sub_meta or sub_matches(sub_meta[sid])]
    print(f"Candidate subscriptions: {len(candidate_ids)}")

    # 3. Users assigned to each subscription
    sub_users, missing = {}, []

    def fetch_sub_users(sid):
        return sid, client.get_all(f"{CV}/subscriptions/{sid}/users")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = [pool.submit(fetch_sub_users, sid) for sid in candidate_ids]
        for n, f in enumerate(as_completed(futs), 1):
            try:
                sid, users = f.result()
                sub_users[sid] = users
            except NotFound as e:
                missing.append(str(e))
            except RuntimeError as e:
                missing.append(str(e))
            if n % 25 == 0 or n == len(candidate_ids):
                total = sum(len(v) for v in sub_users.values())
                print(f"  {n}/{len(candidate_ids)} subscriptions | user rows: {total}")

    # 4. Fill in missing subscription metadata (one lookup per subscription)
    for sid, users in sub_users.items():
        if sid in sub_meta or not users:
            continue
        try:
            for s in client.get_all(f"{CV}/users/{users[0]['id']}/subscriptions"):
                sub_meta.setdefault(s["id"], s)
        except (NotFound, RuntimeError):
            pass

    # 5. Build rows
    rows, seen = [], set()
    for sid, users in sub_users.items():
        s = sub_meta.get(sid, {"id": sid})
        if sid in sub_meta and not sub_matches(s):
            continue
        dt = s.get("discoveryType")
        for u in users:
            key = (u.get("id"), sid)
            if key in seen:
                continue
            seen.add(key)
            rows.append([
                u.get("displayName"), u.get("email"), u.get("username"),
                u.get("country"), u.get("department"), u.get("status"),
                u.get("lastActive"),
                s.get("name"), s.get("applicationName"), s.get("vendor"),
                DISCOVERY_TYPE_NAMES.get(dt, dt), s.get("discoverySource"),
                s.get("totalCostPerMonth"), u.get("id"), sid,
            ])

    header = ["Name", "Email", "Username", "Country", "Department", "User status",
              "User last active", "Subscription", "Application", "Vendor",
              "Discovery type", "Discovery source", "Subscription cost per month",
              "User ID", "Subscription ID"]
    rows.sort(key=lambda r: ((r[0] or "").lower(), r[7] or ""))

    tag = "_".join(x for x in [VENDOR_CONTAINS, APPLICATION_CONTAINS,
                                SUBSCRIPTION_CONTAINS] if x) or "All"
    out = f"SubsPerUser_{tag.replace(' ', '')}_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)

    per_sub = {}
    for r in rows:
        per_sub[r[7] or r[14]] = per_sub.get(r[7] or r[14], 0) + 1
    print("\nRows per subscription:")
    for name, cnt in sorted(per_sub.items(), key=lambda x: -x[1]):
        print(f"  {cnt:>8}  {name}")
    print(f"\nDone - {len(rows)} rows | {len({r[13] for r in rows})} unique users")
    print(f"Output: {out}")
    if missing:
        print(f"Subscriptions that could not be read: {len(missing)} (first 3)")
        for m in missing[:3]:
            print(f"  {m[:200]}")


if __name__ == "__main__":
    main()
