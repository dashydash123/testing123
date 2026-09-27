"""
Flexera One SaaS Management (Snow Atlas) - Subscriptions per user
-----------------------------------------------------------------
Rebuilds the "SaaS > Reports > Subscriptions per user" report via API,
with filters for Vendor, Application, Subscription name and Discovery type.

How it works
  1. Pull ALL applications, keep those whose vendor/name CONTAINS your text
     (case-insensitive, done in Python - so "Microsoft" matches
     "Microsoft Corporation").
  2. Pull every user of those applications (1000 per page).
  3. For each user, pull their subscriptions (parallel threads) and keep the
     ones matching your filters.
  4. Write one row per user-subscription to CSV.

Standard library only - runs on embeddable Python. Fill in CONFIG and run:
    python snow_subscriptions_per_user.py
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

# Filters - "contains", case-insensitive. Leave "" to skip a filter.
VENDOR_CONTAINS = "Microsoft"
APPLICATION_CONTAINS = ""          # e.g. "Teams", "LucidChart"
SUBSCRIPTION_CONTAINS = ""         # e.g. "E3", "Copilot"

# Discovery type filter on the subscription. [] = all types.
#   0 SaaS connector | 1 Manually added | 2 Browser unverified | 3 SSO
#   4 Device         | 7 Browser verified | 8 CASB
DISCOVERY_TYPES = []               # e.g. [0] or [0, 3]

MAX_WORKERS = 8                    # parallel API calls; lower if you hit 429s
TEST_LIMIT = 0                     # e.g. 50 for a quick trial run; 0 = all users
# ======================================================================

BASE_URL = f"https://{REGION}.snowsoftware.io"
CV = "api/saas/consolidated-view/v1"
PAGE_SIZE = 1000

DISCOVERY_TYPE_NAMES = {
    0: "SaaS connector", 1: "Manually added", 2: "Browser unverified",
    3: "SSO", 4: "Device", 7: "Browser verified", 8: "CASB",
}


class SnowClient:
    def __init__(self):
        self.token = None
        self.lock = threading.Lock()
        self.refresh_token()

    def refresh_token(self, stale=None):
        with self.lock:
            if stale is not None and self.token != stale:
                return  # another thread already refreshed it
            body = urllib.parse.urlencode({
                "grant_type": "client_credentials",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            }).encode()
            req = urllib.request.Request(
                f"{BASE_URL}/idp/api/connect/token", data=body, method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
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
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    self.refresh_token(stale=token)
                    continue
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

    def get_all(self, path, params=None):
        params = dict(params or {})
        items, page = [], 1
        while True:
            params.update(page_size=PAGE_SIZE, page_number=page)
            data = self.get(path, params)
            batch = data.get("items") or []
            items.extend(batch)
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

    # 1. Applications matching vendor / app name
    apps = client.get_all(f"{CV}/applications")
    matched = [a for a in apps
               if contains(a.get("vendor"), VENDOR_CONTAINS)
               and contains(a.get("name"), APPLICATION_CONTAINS)]
    print(f"Applications in tenant: {len(apps)} | matching filters: {len(matched)}")

    if not matched:
        vendors = sorted({a.get("vendor") or "" for a in apps})
        print("No match. Some vendor names in your tenant:")
        for v in vendors[:40]:
            print(f"  {v}")
        sys.exit(1)
    for a in sorted(matched, key=lambda x: x.get("name") or ""):
        print(f"  - {a.get('name')}  [{a.get('vendor')}]")

    # 2. Unique users across those applications
    users = {}
    for i, a in enumerate(matched, 1):
        for u in client.get_all(f"{CV}/applications/{a['id']}/users"):
            users.setdefault(u["id"], u)
        print(f"  Users collected after app {i}/{len(matched)}: {len(users)}")

    user_list = list(users.values())
    if TEST_LIMIT:
        user_list = user_list[:TEST_LIMIT]
    print(f"Fetching subscriptions for {len(user_list)} users "
          f"with {MAX_WORKERS} threads ...")

    # 3. Subscriptions per user (parallel)
    rows, failed = [], []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(client.get_all, f"{CV}/users/{u['id']}/subscriptions"): u
                   for u in user_list}
        for n, fut in enumerate(as_completed(futures), 1):
            u = futures[fut]
            try:
                subs = fut.result()
            except Exception as e:
                failed.append((u.get("email"), str(e)))
                continue
            for s in subs:
                if not sub_matches(s):
                    continue
                dt = s.get("discoveryType")
                rows.append([
                    u.get("displayName"), u.get("email"), u.get("username"),
                    u.get("country"), u.get("department"), u.get("status"),
                    u.get("lastActive"),
                    s.get("name"), s.get("applicationName"), s.get("vendor"),
                    DISCOVERY_TYPE_NAMES.get(dt, dt), s.get("discoverySource"),
                    s.get("firstDiscovered"), s.get("lastActivity"),
                    s.get("totalCostPerMonth"), s.get("potentialSavings"),
                    u.get("id"), s.get("id"),
                ])
            if n % 250 == 0 or n == len(user_list):
                print(f"  {n}/{len(user_list)} users done | rows so far: {len(rows)}")

    # 4. Write CSV
    header = ["Name", "Email", "Username", "Country", "Department", "User status",
              "User last active", "Subscription", "Application", "Vendor",
              "Discovery type", "Discovery source", "First discovered",
              "Last activity", "Cost per month", "Potential savings",
              "User ID", "Subscription ID"]
    rows.sort(key=lambda r: ((r[0] or "").lower(), r[7] or ""))

    tag = "_".join(x for x in [VENDOR_CONTAINS, APPLICATION_CONTAINS,
                                SUBSCRIPTION_CONTAINS] if x) or "All"
    out = f"SubsPerUser_{tag.replace(' ', '')}_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)

    print(f"\nDone - {len(rows)} rows, "
          f"{len({r[16] for r in rows})} users with matching subscriptions")
    print(f"Output: {out}")
    if failed:
        print(f"Failed users: {len(failed)} (first 5)")
        for email, err in failed[:5]:
            print(f"  {email}: {err[:150]}")


if __name__ == "__main__":
    main()
