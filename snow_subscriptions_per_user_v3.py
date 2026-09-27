"""
Flexera One SaaS Management (Snow Atlas) - Subscriptions per user  (v3)
-----------------------------------------------------------------------
Same approach as v2 (enumerate by SUBSCRIPTION, not by app-user, so the
row count matches the "Subscriptions per user" report), but every setting
- credentials, filters, tuning - now lives in config.env next to this
script instead of being hardcoded here.

Setup
  1. Edit config.env: fill in CLIENT_ID / CLIENT_SECRET and your filters.
  2. Run:  python snow_subscriptions_per_user_v3.py
     (or:  python snow_subscriptions_per_user_v3.py path\\to\\other.env)

Filters in config.env
  VENDOR_CONTAINS / VENDOR_EXCLUDES        - vendor name, "contains" match
  APPLICATION_CONTAINS                      - application name, "contains"
  SUBSCRIPTION_CONTAINS                     - subscription name, "contains"
  DISCOVERY_TYPES                           - comma list of type numbers, or empty for all
  All *_CONTAINS / *_EXCLUDES values may be comma-separated lists; a row
  matches CONTAINS if it matches ANY value in the list, and is dropped by
  EXCLUDES if it matches ANY value there.

Standard library only - no pip installs needed.
"""

import csv
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# ============================ load config.env ============================
def load_env_file(path):
    values = {}
    if not os.path.exists(path):
        return values
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            values[key] = val
    return values


def csv_list(value):
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def int_list(value):
    out = []
    for v in csv_list(value):
        try:
            out.append(int(v))
        except ValueError:
            print(f"  Warning: ignoring non-numeric discovery type '{v}'")
    return out


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(SCRIPT_DIR, "config.env")
ENV = load_env_file(ENV_PATH)

# Environment variables (if set) take priority over config.env; both are optional.
def cfg(key, default=""):
    return os.environ.get(key, ENV.get(key, default))


CLIENT_ID = cfg("CLIENT_ID")
CLIENT_SECRET = cfg("CLIENT_SECRET")
REGION = cfg("REGION", "westeurope")

VENDOR_CONTAINS = csv_list(cfg("VENDOR_CONTAINS"))
VENDOR_EXCLUDES = csv_list(cfg("VENDOR_EXCLUDES"))
APPLICATION_CONTAINS = csv_list(cfg("APPLICATION_CONTAINS"))
SUBSCRIPTION_CONTAINS = csv_list(cfg("SUBSCRIPTION_CONTAINS"))
DISCOVERY_TYPES = int_list(cfg("DISCOVERY_TYPES"))

SEED_USERS_PER_APP = int(cfg("SEED_USERS_PER_APP", "200") or 200)
MAX_WORKERS = int(cfg("MAX_WORKERS", "8") or 8)
# ===========================================================================

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


def contains_any(value, needles):
    if not needles:
        return True
    v = str(value or "").lower()
    return any(n.lower() in v for n in needles)


def excluded_any(value, needles):
    if not needles:
        return False
    v = str(value or "").lower()
    return any(n.lower() in v for n in needles)


def app_matches(a):
    return (contains_any(a.get("vendor"), VENDOR_CONTAINS)
            and contains_any(a.get("name"), APPLICATION_CONTAINS)
            and not excluded_any(a.get("vendor"), VENDOR_EXCLUDES))


def sub_matches(s):
    return (contains_any(s.get("vendor"), VENDOR_CONTAINS)
            and contains_any(s.get("applicationName"), APPLICATION_CONTAINS)
            and contains_any(s.get("name"), SUBSCRIPTION_CONTAINS)
            and not excluded_any(s.get("vendor"), VENDOR_EXCLUDES)
            and (not DISCOVERY_TYPES or s.get("discoveryType") in DISCOVERY_TYPES))


def main():
    if not CLIENT_ID or not CLIENT_SECRET or "PASTE_" in CLIENT_ID or "PASTE_" in CLIENT_SECRET:
        sys.exit(f"Fill in CLIENT_ID and CLIENT_SECRET in {ENV_PATH}")

    client = SnowClient()
    print(f"Authenticated against {BASE_URL}  (config: {ENV_PATH})")
    if VENDOR_CONTAINS:
        print(f"  Vendor contains:     {VENDOR_CONTAINS}")
    if VENDOR_EXCLUDES:
        print(f"  Vendor excludes:     {VENDOR_EXCLUDES}")
    if APPLICATION_CONTAINS:
        print(f"  Application contains: {APPLICATION_CONTAINS}")
    if SUBSCRIPTION_CONTAINS:
        print(f"  Subscription contains: {SUBSCRIPTION_CONTAINS}")
    if DISCOVERY_TYPES:
        print(f"  Discovery types:     {DISCOVERY_TYPES}")

    # 1. Matching applications
    apps = client.get_all(f"{CV}/applications")
    matched = [a for a in apps if app_matches(a)]
    print(f"Applications: {len(apps)} | matching: {len(matched)}")
    if not matched:
        sys.exit("No applications match the vendor/application filter.")

    # 2. Discover subscription IDs (+ metadata where available)
    sub_ids = set()
    sub_meta = {}

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
            except (NotFound, RuntimeError) as e:
                missing.append(str(e))
            if n % 25 == 0 or n == len(candidate_ids):
                total = sum(len(v) for v in sub_users.values())
                print(f"  {n}/{len(candidate_ids)} subscriptions | user rows: {total}")

    # 4. Fill in missing subscription metadata
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

    tag = "_".join(VENDOR_CONTAINS + APPLICATION_CONTAINS + SUBSCRIPTION_CONTAINS) or "All"
    out = os.path.join(SCRIPT_DIR,
                        f"SubsPerUser_{tag.replace(' ', '')}_{datetime.now():%Y%m%d_%H%M%S}.csv")
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
