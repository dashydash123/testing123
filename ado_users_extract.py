import os
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

print(df.shape)
print(df.head())
df.to_csv("tblADO_user_master_table.csv", index=False, encoding="utf-8-sig")
