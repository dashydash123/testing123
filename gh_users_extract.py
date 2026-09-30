"""
gh_users_extract.py
Extracts dbo.tblGH_users (GitHub users) from the DevSecOps Azure SQL DB to CSV.
All credentials/config are read from .env in the same folder.

Requires:  python -m pip install pyodbc python-dotenv
           + Microsoft "ODBC Driver 18 for SQL Server" (or 17)
"""
import csv
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

        cur.execute(f"SELECT * FROM {fq_table}")
        columns = [c[0] for c in cur.description]
        print(f"[query] Columns: {', '.join(columns)}")

        i_acc = col_index(columns, LAST_ACCESSED_COL)
        i_role = col_index(columns, ROLE_COL)
        i_org = col_index(columns, ORG_COL)
        i_email = col_index(columns, EMAIL_COL)
        bad_email, bad_email_samples = 0, []
        sep = env("LICENSE_SEPARATOR", " - ")
        id_fields = [c.strip() for c in env("ID_FIELDS", "username").split(",") if c.strip()]
        i_ids = [col_index(columns, c) for c in id_fields]
        seen_ids, dup_ids, blank_ids = set(), 0, 0

        written = removed = blank_acc = 0
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
                    # 1. drop never-accessed rows (0001-01-01)
                    if is_null_date(r[i_acc]):
                        removed += 1
                        continue
                    # 1b. drop rows whose email is not a real address (no @ etc.)
                    if not is_valid_email(r[i_email]):
                        bad_email += 1
                        if len(bad_email_samples) < 10:
                            bad_email_samples.append(f"{r[i_role]} | {r[i_org]} | {r[i_email]!r}")
                        continue
                    row = [to_cell(v) for v in r]
                    # 2. last_accessed -> ISO 8601 UTC, like last_updated
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
                print(f"[write] {written + removed:,}/{expected:,} processed", end="\r")

    print()
    try:
        os.replace(tmp_file, out_file)
    except PermissionError:
        sys.exit(f"[save] {out_file.name} is open (probably in Excel). Close it and re-run.\n"
                 f"       This run's data is kept in {tmp_file}")
    status = "OK" if written + removed + bad_email == expected else "MISMATCH"
    print(f"[filter] Removed {removed:,} rows with {LAST_ACCESSED_COL} = 0001-01-01")
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
    print(f"[done] {status}: wrote {written:,} rows (+{removed + bad_email:,} removed = {expected:,}) -> {out_file}")
    if status != "OK":
        sys.exit(1)


if __name__ == "__main__":
    main()
