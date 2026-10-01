import os
import uuid
from datetime import datetime
import pyodbc
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

conn = pyodbc.connect(
    "DRIVER={ODBC Driver 18 for SQL Server};"
    f"SERVER=tcp:{os.getenv('SQL_SERVER')},1433;"
    f"DATABASE={os.getenv('SQL_DATABASE')};"
    f"UID={os.getenv('SQL_USERNAME')};"
    "PWD={" + os.getenv("SQL_PASSWORD").replace("}", "}}") + "};"
    "Encrypt=yes;TrustServerCertificate=no;"
)

df = pd.read_sql("SELECT * FROM dbo.tblADO_user_master_table", conn)
conn.close()
print(f"Rows from SQL: {len(df)}")

# Line breaks inside text values (e.g. in projects) show up as broken/empty rows in Excel
df = df.replace({r"[\r\n]+": " "}, regex=True)

# Drop completely empty rows
before = len(df)
df = df[~df.apply(lambda r: r.astype(str).str.strip().isin(["", "None", "nan", "NaT"]).all(), axis=1)]
print(f"Removed empty rows: {before - len(df)}")

# Drop invalid emails (blank or no @)
before = len(df)
df = df[df["email"].astype(str).str.contains("@", na=False)]
print(f"Removed invalid emails: {before - len(df)}")

# last_accessed -> ISO 8601 UTC (2026-09-25T14:03:11Z); 0001-01-01 left as it is
def to_iso(v):
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return ""
    if isinstance(v, datetime):
        return str(v) if v.year <= 1 else v.strftime("%Y-%m-%dT%H:%M:%SZ")
    s = str(v).strip()
    if s.startswith("0001-01-01"):
        return s
    parsed = pd.to_datetime(s, errors="coerce")
    return s if pd.isna(parsed) else parsed.strftime("%Y-%m-%dT%H:%M:%SZ")

df["last_accessed"] = df["last_accessed"].map(to_iso)

# user_id -> 36-char ID, unique per user + org + licence tier, same every run
def make_id(ado_user_id, org, license_type):
    key = f"{ado_user_id}|{org}|{license_type}".strip().lower()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))

df.insert(1, "orig_user_id", df["user_id"])
df["user_id"] = [make_id(a, o, l) for a, o, l in
                 zip(df["ado_user_id"], df["org"], df["license_type"])]

dupes = df[df["user_id"].duplicated(keep=False)]
if not dupes.empty:
    print(f"WARNING: {len(dupes)} rows share a user+org+licence combination:")
    print(dupes[["orig_user_id", "email", "org", "license_type"]])

print(f"Rows written: {len(df)}")
print(df[["user_id", "email", "org", "license_type", "last_accessed"]].head())
df.to_csv("tblADO_user_master_table.csv", index=False, encoding="utf-8-sig")
