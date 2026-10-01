import os
import uuid
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

# 1. last_accessed -> ISO 8601 UTC, e.g. 2026-09-25T14:03:11Z (blank if empty)
df["last_accessed"] = (
    pd.to_datetime(df["last_accessed"], errors="coerce", utc=True)
      .dt.strftime("%Y-%m-%dT%H:%M:%SZ")
      .fillna("")
)

# 2. user_id -> 36-char ID, unique per user + org + licence tier, same every run
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

print(df.shape)
print(df[["user_id", "email", "org", "license_type", "last_accessed"]].head())
df.to_csv("tblADO_user_master_table.csv", index=False, encoding="utf-8-sig")
