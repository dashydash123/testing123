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
from datetime import datetime
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


def main():
    fq_table, table = qualified_table()
    out_dir = BASE_DIR / env("OUTPUT_DIR", "output")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{table}_{datetime.now():%Y%m%d_%H%M%S}.csv"

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

        written = 0
        # utf-8-sig so Excel opens accented names (e.g. Pedrilho, Panzieri) correctly
        with open(out_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            while True:
                rows = cur.fetchmany(BATCH_SIZE)
                if not rows:
                    break
                writer.writerows([to_cell(v) for v in r] for r in rows)
                written += len(rows)
                print(f"[write] {written:,}/{expected:,}", end="\r")

    print()
    status = "OK" if written == expected else "MISMATCH"
    print(f"[done] {status}: wrote {written:,} rows -> {out_file}")
    if status != "OK":
        sys.exit(1)


if __name__ == "__main__":
    main()
