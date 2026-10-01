"""
gh_users_to_flexera.py  -  GitHub Enterprise users -> Flexera (Snow Atlas) SaaS import, one run.

  1. Extract  dbo.tblGH_users from the DevSecOps Azure SQL DB
  2. Clean    - drop rows with an invalid email (no @ etc.)
              - last_accessed -> ISO 8601 (2026-09-29T10:22:33Z); 0001-01-01 -> blank
              - License = role + ' - ' + org
              - user_id = stable UUID from username + org (one per org membership)
     -> output/tblGH_users.csv (overwritten every run)
  3. Build    - output/gh_flexera_assignments.csv : email, user_id, License (all users)
              - output/gh_flexera_activities.csv  : user_id, last_accessed
                (never-accessed users left out -> Flexera marks them 'No activity')
  4. Push     both files: token -> upload URL -> PUT to blob -> trigger import

Flexera mappings (GitHub Enterprise, UAT)
  User assignments (FLEXERA_ASSIGNMENTS_CONFIG_ID)
    Email address of the user = email | Unique user identifier = user_id | Subscription name = License
  User activities (FLEXERA_ACTIVITIES_CONFIG_ID)
    Unique user identifier = user_id | Activity name + recorded value = last_accessed

All credentials, API URLs and config IDs come from .env in the same folder.
Set FLEXERA_PUSH=no in .env to extract and build the files without pushing.

Requires:  python -m pip install pyodbc python-dotenv requests
           + Microsoft "ODBC Driver 18 for SQL Server" (or 17)
"""
import csv
import json
import os
import re
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

try:
    import pyodbc
except ImportError:
    sys.exit("pyodbc not installed ->  python -m pip install pyodbc")
try:
    import requests
except ImportError:
    sys.exit("requests not installed ->  python -m pip install requests")
try:
    from dotenv import load_dotenv
except ImportError:
    sys.exit("python-dotenv not installed ->  python -m pip install python-dotenv")

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

SAFE_IDENT = re.compile(r"^[A-Za-z0-9_]+$")
BATCH_SIZE = 5000


def env(key, default=None, required=False):
    val = os.getenv(key, default)
    if required and (not val or val.startswith("PASTE_")):
        sys.exit(f"[config] {key} is missing in .env")
    return val


def pick_driver(preferred):
    if preferred:
        return preferred
    installed = pyodbc.drivers()
    for d in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
        if d in installed:
            return d
    sys.exit(
        "[config] No SQL Server ODBC driver found.\n"
        f"Installed drivers: {installed}\n"
        "Install 'ODBC Driver 18 for SQL Server' from Microsoft, or set SQL_DRIVER in .env."
    )


def odbc_value(v):
    """Brace-wrap so special characters (; & ^ @ etc.) in values are safe."""
    return "{" + v.replace("}", "}}") + "}"


def build_conn_str():
    server = env("SQL_SERVER", required=True)
    port = env("SQL_PORT", "1433")
    return ";".join([
        f"DRIVER={{{pick_driver(env('SQL_DRIVER'))}}}",
        f"SERVER=tcp:{server},{port}",
        f"DATABASE={odbc_value(env('SQL_DATABASE', required=True))}",
        f"UID={odbc_value(env('SQL_USERNAME', required=True))}",
        f"PWD={odbc_value(env('SQL_PASSWORD', required=True))}",
        f"Encrypt={env('SQL_ENCRYPT', 'yes')}",
        f"TrustServerCertificate={env('SQL_TRUST_SERVER_CERT', 'no')}",
        f"Connection Timeout={env('SQL_TIMEOUT', '30')}",
    ]) + ";"


def qualified_table():
    schema, table = env("SQL_SCHEMA", "dbo"), env("SQL_TABLE", required=True)
    for name in (schema, table):
        if not SAFE_IDENT.match(name):
            sys.exit(f"[config] Invalid schema/table name: {name!r}")
    return f"[{schema}].[{table}]", table


def to_cell(v):
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    return v


# ── Transform settings ──────────────────────────────────────────────
LAST_ACCESSED_COL = "last_accessed"
ROLE_COL, ORG_COL, LICENSE_COL = "role", "org", "License"
ID_COL = "user_id"
EMAIL_COL = "email"
# something@domain.tld  -> no spaces, exactly one @, a dot in the domain
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]{2,}$")


def is_valid_email(v):
    return isinstance(v, str) and bool(EMAIL_RE.match(v.strip()))
# Fixed namespace: the same username always gets the same user_id, run after run
ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "github.users.flexera")


def make_user_id(key):
    return str(uuid.uuid5(ID_NAMESPACE, key)) if key else ""
ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"          # same shape as last_updated
_STR_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y")


def is_null_date(v):
    """True for the 0001-01-01 'never accessed' placeholder."""
    if isinstance(v, (datetime, date)):
        return v.year == 1
    return isinstance(v, str) and v.strip().startswith("0001-01-01")


def to_iso_utc(v):
    """Convert datetime/date/string to 2026-09-29T10:22:33Z. Blank stays blank."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return ""
    if isinstance(v, str):
        raw = v.strip().replace("Z", "").split("+")[0]
        raw = raw[:26]  # trim 7-digit SQL fractions to what strptime accepts
        for fmt in _STR_FORMATS:
            try:
                v = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"Unrecognised {LAST_ACCESSED_COL} value: {v!r}")
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v.strftime(ISO_FMT)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day).strftime(ISO_FMT)
    raise ValueError(f"Unsupported {LAST_ACCESSED_COL} type: {type(v).__name__}")


def col_index(columns, name):
    lookup = {c.lower(): i for i, c in enumerate(columns)}
    if name.lower() not in lookup:
        sys.exit(f"[transform] Column '{name}' not found. Columns: {columns}")
    return lookup[name.lower()]


# ══════════════════════════ FLEXERA PUSH ══════════════════════════
# Headers must match the CSV field names in the Flexera mappings EXACTLY
ASSIGNMENT_COLS = ["email", "user_id", "License"]
ACTIVITY_COLS = ["user_id", "last_accessed"]


def cfg(key, default=None):
    val = (os.getenv(key) or default or "").strip()
    if not val or val.startswith("PASTE_"):
        sys.exit(f"[config] {key} is missing in .env")
    return val


# ── Build the two import files ──────────────────────────────────────
def read_extract(path):
    if not path.exists():
        sys.exit(f"[flexera] Extract not found: {path}. Run gh_users_extract.py first.")
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in set(ASSIGNMENT_COLS + ACTIVITY_COLS) if c not in reader.fieldnames]
        if missing:
            sys.exit(f"[flexera] {path.name} is missing column(s): {', '.join(missing)}")
        return list(reader)


def build_assignments(rows):
    """One row per user + licence. Exact repeats are dropped."""
    seen, out = set(), []
    for r in rows:
        key = (r["user_id"], r["License"])
        if not r["user_id"] or key in seen:
            continue
        seen.add(key)
        out.append({c: r[c] for c in ASSIGNMENT_COLS})
    return out


def build_activities(rows):
    """One row per user_id with a real last_accessed.
    Never-accessed users (blank last_accessed) are left out; Flexera marks them 'No activity'."""
    latest = {}
    for r in rows:
        uid, ts = r["user_id"], r["last_accessed"]
        if uid and ts and (uid not in latest or ts > latest[uid]):  # ISO strings sort correctly
            latest[uid] = ts
    return [{"user_id": u, "last_accessed": t} for u, t in latest.items()]


def write_csv(path, rows, cols):
    # plain UTF-8, no BOM, so Flexera reads the first header name correctly
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


# ── Flexera API ─────────────────────────────────────────────────────
def check(resp, step):
    if not resp.ok:
        sys.exit(f"[flexera] {step} failed: HTTP {resp.status_code} {resp.text[:500]}")


def get_token():
    resp = requests.post(
        cfg("FLEXERA_TOKEN_URL"),
        data={"grant_type": "client_credentials",
              "client_id": cfg("FLEXERA_CLIENT_ID"),
              "client_secret": cfg("FLEXERA_CLIENT_SECRET")},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code in (400, 401):
        sys.exit("[flexera] Token refused. Check FLEXERA_CLIENT_ID / FLEXERA_CLIENT_SECRET.")
    check(resp, "Token request")
    print("[flexera] Access token obtained")
    return resp.json()["access_token"]


def push(token, csv_path, config_id, label):
    print(f"[flexera] Pushing {label} ({csv_path.name})")
    auth = {"Authorization": f"Bearer {token}"}

    resp = requests.post(cfg("FLEXERA_UPLOAD_URL"), headers=auth, timeout=30)
    check(resp, "Upload URL request")
    data = resp.json()
    print(f"          upload URL received (fileId={data.get('fileId')})")

    with open(csv_path, "rb") as f:
        resp = requests.put(data["url"], data=f, timeout=120,
                            headers={"x-ms-blob-type": "BlockBlob", "Content-Type": "text/csv"})
    check(resp, "Blob upload")
    print(f"          uploaded to blob (HTTP {resp.status_code})")

    resp = requests.post(cfg("FLEXERA_IMPORT_URL").replace("{config_id}", config_id),
                         headers={**auth, "Content-Type": "application/json"},
                         data=json.dumps({"fileId": data["fileId"]}), timeout=60)
    check(resp, "Import trigger")
    print(f"          import task created (HTTP {resp.status_code})")


def push_all(extract_csv=None):
    out_dir = BASE_DIR / (os.getenv("OUTPUT_DIR") or "output")
    extract_csv = Path(extract_csv) if extract_csv else out_dir / (os.getenv("OUTPUT_FILE") or "tblGH_users.csv")
    rows = read_extract(extract_csv)

    assignments = build_assignments(rows)
    activities = build_activities(rows)
    users = len({r["user_id"] for r in assignments})

    assign_csv = out_dir / "gh_flexera_assignments.csv"
    activity_csv = out_dir / "gh_flexera_activities.csv"
    write_csv(assign_csv, assignments, ASSIGNMENT_COLS)
    write_csv(activity_csv, activities, ACTIVITY_COLS)

    print(f"\n[flexera] Assignments: {len(assignments):,} rows ({users:,} unique user_ids)")
    never = len({r["user_id"] for r in rows if r["user_id"] and not r["last_accessed"]})
    print(f"[flexera] Activities : {len(activities):,} rows "
          f"({never:,} never-accessed left out -> 'No activity' in Flexera)")

    token = get_token()
    if assignments:
        push(token, assign_csv, cfg("FLEXERA_ASSIGNMENTS_CONFIG_ID"), "user assignments")
    if activities:
        push(token, activity_csv, cfg("FLEXERA_ACTIVITIES_CONFIG_ID"), "user activities")
    print("[flexera] Done. Check Flexera > Data imports > GitHub Enterprise.")


# ══════════════════════════ PIPELINE ══════════════════════════════
def main():
    fq_table, table = qualified_table()
    out_dir = BASE_DIR / env("OUTPUT_DIR", "output")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / env("OUTPUT_FILE", f"{table}.csv")
    tmp_file = out_file.with_suffix(out_file.suffix + ".tmp")

    print(f"[connect] {env('SQL_SERVER')} / {env('SQL_DATABASE')} as {env('SQL_USERNAME')}")
    try:
        conn = pyodbc.connect(build_conn_str())
    except pyodbc.Error as e:
        sys.exit(f"[connect] Failed: {e}")

    with conn:
        cur = conn.cursor()
        expected = cur.execute(f"SELECT COUNT(*) FROM {fq_table}").fetchone()[0]
        print(f"[query] {fq_table}: {expected:,} rows on server")
        if expected == 0:
            sys.exit(f"[stop] {fq_table} is empty on the server (source refresh pending or failed?).\n"
                     f"       Nothing written or pushed - last good {out_file.name} kept.")

        cur.execute(f"SELECT * FROM {fq_table}")
        columns = [c[0] for c in cur.description]
        print(f"[query] Columns: {', '.join(columns)}")

        i_acc = col_index(columns, LAST_ACCESSED_COL)
        i_role = col_index(columns, ROLE_COL)
        i_org = col_index(columns, ORG_COL)
        i_email = col_index(columns, EMAIL_COL)
        bad_email, bad_email_samples = 0, []
        sep = env("LICENSE_SEPARATOR", " - ")
        id_fields = [c.strip() for c in env("ID_FIELDS", "username,org").split(",") if c.strip()]
        i_ids = [col_index(columns, c) for c in id_fields]
        seen_ids, dup_ids, blank_ids = set(), 0, 0

        written = never = blank_acc = 0
        # utf-8-sig so Excel opens accented names correctly
        # write to a temp file first; the real file is only replaced on success
        with open(tmp_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([ID_COL] + columns + [LICENSE_COL])
            while True:
                rows = cur.fetchmany(BATCH_SIZE)
                if not rows:
                    break
                out = []
                for r in rows:
                    # 1. drop rows whose email is not a real address (no @ etc.)
                    if not is_valid_email(r[i_email]):
                        bad_email += 1
                        if len(bad_email_samples) < 10:
                            bad_email_samples.append(f"{r[i_role]} | {r[i_org]} | {r[i_email]!r}")
                        continue
                    row = [to_cell(v) for v in r]
                    # 2. last_accessed -> ISO 8601 UTC, like last_updated.
                    #    0001-01-01 = never accessed: row is KEPT (user assignment)
                    #    but left blank, so it is not sent as an activity.
                    if is_null_date(r[i_acc]):
                        never += 1
                        row[i_acc] = ""
                    else:
                        row[i_acc] = to_iso_utc(r[i_acc])
                        if not row[i_acc]:
                            blank_acc += 1
                    # 3. License = role + sep + org
                    parts = [str(r[i_role] or "").strip(), str(r[i_org] or "").strip()]
                    row.append(sep.join(p for p in parts if p))
                    # 4. user_id = stable UUID from ID_FIELDS (case-insensitive)
                    key = "|".join(str(r[i] or "").strip().lower() for i in i_ids)
                    uid = make_user_id(key.strip("|") and key)
                    if not uid:
                        blank_ids += 1
                    elif uid in seen_ids:
                        dup_ids += 1
                    seen_ids.add(uid)
                    out.append([uid] + row)
                writer.writerows(out)
                written += len(out)
                print(f"[write] {written + bad_email:,}/{expected:,} processed", end="\r")

    print()
    try:
        os.replace(tmp_file, out_file)
    except PermissionError:
        sys.exit(f"[save] {out_file.name} is open (probably in Excel). Close it and re-run.\n"
                 f"       This run's data is kept in {tmp_file}")
    status = "OK" if written + bad_email == expected else "MISMATCH"
    print(f"[info] Kept {never:,} never-accessed rows (0001-01-01): in user assignments, "
          f"not in activities; {LAST_ACCESSED_COL} left blank")
    print(f"[filter] Removed {bad_email:,} rows with an invalid {EMAIL_COL}")
    for sample in bad_email_samples:
        print(f"         e.g. {sample}")
    if blank_ids:
        print(f"[warn] {blank_ids:,} rows have no {'/'.join(id_fields)} -> blank {ID_COL}")
    if dup_ids:
        print(f"[warn] {dup_ids:,} rows share a {ID_COL} with an earlier row "
              f"(same {'/'.join(id_fields)} appears more than once)")
    if blank_acc:
        print(f"[warn] {blank_acc:,} kept rows have an empty {LAST_ACCESSED_COL}")
    print(f"[done] {status}: wrote {written:,} rows (+{bad_email:,} removed = {expected:,}) -> {out_file}")
    if status != "OK":
        sys.exit(1)

    if (os.getenv("FLEXERA_PUSH") or "no").strip().lower() == "yes":
        push_all(out_file)
    else:
        print("[flexera] FLEXERA_PUSH is not 'yes' - nothing sent to Flexera")


if __name__ == "__main__":
    main()
