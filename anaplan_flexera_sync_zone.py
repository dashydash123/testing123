#!/usr/bin/env python3
"""Anaplan -> Flexera (Snow Atlas) SaaS import, end to end.

1. Authenticates with Anaplan (saved refresh token, or device-code flow).
2. Runs the configured Anaplan export and downloads every chunk to test.csv.
3. Post-processes the export (A1 metadata row, Email ID header, ISO dates,
   Inactive Days / 90-day flag).
4. Builds the two Flexera CSVs with headers matching the Flexera mappings.
5. Pushes both CSVs to Flexera: token -> upload URL -> blob PUT -> import.

Built to be scheduled: every output file keeps the same name and is
overwritten on each run.
"""

import csv
import hashlib
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import requests

# ===========================================================================
# CONFIGURATION
# ===========================================================================

# ---- Anaplan --------------------------------------------------------------
WORKSPACE_ID      = ""
MODEL_ID          = ""
EXPORT_ID         = ""
ANAPLAN_CLIENT_ID = ""   # <-- Anaplan OAuth client ID (from the original Anaplan script)
SCOPE = "openid profile email offline_access"

OAUTH_DEVICE_CODE_URL = "https://us1a.app.anaplan.com/oauth/device/code"
OAUTH_TOKEN_URL       = "https://us1a.app.anaplan.com/oauth/token"
ANAPLAN_API_BASE_URL  = "https://api.anaplan.com/2/0"

# ---- Flexera (UAT) - Application Registration -----------------------------
CLIENT_ID     = ""   # <-- Flexera Application Registration Client ID
CLIENT_SECRET = ""   # <-- Flexera Application Registration Client Secret

BASE        = "https://snowatlas-westeurope.flexera.eu"
TOKEN_URL   = f"{BASE}/idp/api/connect/token"
USERS_URL   = f"{BASE}/api/saas/consolidated-view/v1/users"
UPLOAD_URL  = f"{BASE}/api/saas/import/v1/uploads"
# IMPORT_URL = f"{BASE}/api/saas/import/v1/configs/{{CONFIG_ID}}/records/import"

ASSIGNMENTS_CONFIG_ID = "3298061a-7c0b-4218-bb95-739f249e3a56"   # user assignments
ACTIVITIES_CONFIG_ID  = "99429703-8c03-4980-ac89-fb830fa855bb"   # user activities

# ---- Files (fixed names -> overwritten every run) -------------------------
BASE_DIRECTORY  = Path(r"C:\Users\C565668\Downloads\Scripts\Anaplan")
TOKEN_FILE      = BASE_DIRECTORY / "token.json"
OUTPUT_FILE     = BASE_DIRECTORY / "test.csv"                 # Anaplan export
ASSIGNMENTS_CSV = BASE_DIRECTORY / "anaplan_license.csv"      # -> Flexera
ACTIVITIES_CSV  = BASE_DIRECTORY / "anaplan_activities.csv"   # -> Flexera

# ---- Run switches ---------------------------------------------------------
RUN_ANAPLAN_EXPORT  = True   # False = reuse the existing test.csv
PUSH_TO_FLEXERA     = True   # False = build files only, no Flexera calls
ONLY_ENABLED_USERS  = True   # drop users whose User State is not 'Enabled'
ANAPLAN_TENANT_KEY  = "anaplan"   # make_user_id() key when User ID is blank

# ---- Anaplan CSV post-processing ------------------------------------------
LAST_LOGIN_COLUMN    = "Last Active Date"
INACTIVE_DAYS_COLUMN = "Inactive Days"
INACTIVE_COLUMN      = "Not Logged In Over 90 Days"
INACTIVE_DAYS        = 90
ISO_DATETIME_COLUMNS = ("Account Creation Date", "Last Active Date")

# ---- Anaplan export header -> internal name --------------------------------
SOURCE_COLUMNS = {
    "Email ID":              "email",
    "User ID":               "anaplan_id",
    "License Type":          "license_type",
    "User State":            "user_state",
    "Assigned Zone":         "zone",
    "Account Creation Date": "created",
    "Last Active Date":      "last_active",
}
REQUIRED_SOURCE = ["email", "license_type", "last_active"]

# ---- Assigned Zone normalisation ------------------------------------------
# Every zone collapses to one of these seven codes; blank -> GHQ.
# License Type sent to Flexera = "<License Type> <Zone>", e.g. "Enterprise User SAZ"
CANONICAL_ZONES = ("GHQ", "NAZ", "MAZ", "SAZ", "APAC", "EUR", "AFR")
DEFAULT_ZONE = "GHQ"          # used for blank zones
UNMAPPED_ZONE_DEFAULT = "GHQ" # used (with a warning) for zones not recognised

# Alias -> code. Matching is case-, space- and punctuation-insensitive and
# works on whole words, so "GHQ SOLUTIONS", "ghq-fp&a", "Global HQ" -> GHQ.
# Add new variants here if the warning lists unmapped zones.
ZONE_ALIASES = {
    # GHQ
    "GHQ": "GHQ", "HQ": "GHQ", "GLOBAL": "GHQ", "GLOBAL HQ": "GHQ",
    "GLOBAL HEADQUARTERS": "GHQ", "GLOBAL HEAD QUARTERS": "GHQ",
    "HEADQUARTERS": "GHQ", "CORPORATE": "GHQ", "GCC": "GHQ",
    # NAZ
    "NAZ": "NAZ", "NORTH AMERICA": "NAZ", "NORTH AMERICAN": "NAZ",
    "NORTH AMERICA ZONE": "NAZ", "NA ZONE": "NAZ", "USA": "NAZ", "US": "NAZ",
    "CANADA": "NAZ",
    # MAZ
    "MAZ": "MAZ", "MIDDLE AMERICA": "MAZ", "MIDDLE AMERICAS": "MAZ",
    "MIDDLE AMERICAS ZONE": "MAZ", "MIDDLE AMERICA ZONE": "MAZ",
    "CENTRAL AMERICA": "MAZ", "MEXICO": "MAZ",
    # SAZ
    "SAZ": "SAZ", "SOUTH AMERICA": "SAZ", "SOUTH AMERICAN": "SAZ",
    "SOUTH AMERICA ZONE": "SAZ", "SA ZONE": "SAZ", "BRAZIL": "SAZ",
    "SOUTH ASIA": "SAZ", "SOUTH ASIA ZONE": "SAZ",   # per requested example
    # APAC
    "APAC": "APAC", "ASIA PACIFIC": "APAC", "ASIA PAC": "APAC", "ASPAC": "APAC",
    "AP": "APAC", "ASIA": "APAC", "APAC NORTH": "APAC", "APAC SOUTH": "APAC",
    "CHINA": "APAC",
    # EUR
    "EUR": "EUR", "EU": "EUR", "EURO": "EUR", "EUROPE": "EUR",
    "EUROPE ZONE": "EUR", "EUROPEAN": "EUR",
    # AFR
    "AFR": "AFR", "AF": "AFR", "AFRICA": "AFR", "AFRICA ZONE": "AFR",
    "AFRICAN": "AFR",
}
BLANK_ZONE_VALUES = {"", "NA", "N A", "NONE", "NULL", "NAN", "BLANK", "UNKNOWN"}

# ---- Flexera CSV headers (must match each import's Mapping screen) ---------
# Assignments: Email address = Email ID | Unique user identifier = User ID |
#   Subscription name = License Type | User account creation = Account
#   Creation Date | User account last updated = Last Active Date
ASSIGNMENTS_TEMPLATE = ["Email ID", "User ID", "License Type",
                        "Account Creation Date", "Last Active Date"]
# Activities: Unique user identifier = User ID | Activity or event name =
#   Last Active Date | Activity recorded value = Last Active Date
ACTIVITIES_TEMPLATE = ["User ID", "Last Active Date"]


# ===========================================================================
# CONFIG VALIDATION
# ===========================================================================

def _mask(value: str) -> str:
    value = value or ""
    return value if len(value) <= 8 else f"{value[:4]}...{value[-4:]}"

def validate_config() -> None:
    """Stop early with a clear message instead of a NameError / HTTP error."""
    global WORKSPACE_ID, MODEL_ID, EXPORT_ID, ANAPLAN_CLIENT_ID, CLIENT_ID, CLIENT_SECRET
    # Strip stray spaces / quotes picked up when pasting values
    WORKSPACE_ID, MODEL_ID, EXPORT_ID, ANAPLAN_CLIENT_ID, CLIENT_ID, CLIENT_SECRET = (
        str(v).strip().strip('"').strip("'") for v in
        (WORKSPACE_ID, MODEL_ID, EXPORT_ID, ANAPLAN_CLIENT_ID, CLIENT_ID, CLIENT_SECRET)
    )
    required = {}
    if RUN_ANAPLAN_EXPORT:
        required.update({"WORKSPACE_ID": WORKSPACE_ID, "MODEL_ID": MODEL_ID,
                         "EXPORT_ID": EXPORT_ID, "ANAPLAN_CLIENT_ID": ANAPLAN_CLIENT_ID})
    if PUSH_TO_FLEXERA:
        required.update({"CLIENT_ID (Flexera)": CLIENT_ID,
                         "CLIENT_SECRET (Flexera)": CLIENT_SECRET})
    missing = [k for k, v in required.items() if not v or v.startswith("*")]
    if missing:
        sys.exit(f"Fill in these values in the CONFIGURATION section: {', '.join(missing)}")
    if RUN_ANAPLAN_EXPORT and PUSH_TO_FLEXERA and ANAPLAN_CLIENT_ID == CLIENT_ID:
        sys.exit("ANAPLAN_CLIENT_ID and the Flexera CLIENT_ID are identical. "
                 "ANAPLAN_CLIENT_ID must be the Anaplan OAuth client ID; "
                 "CLIENT_ID must be the Flexera Application Registration ID.")
    if RUN_ANAPLAN_EXPORT:
        print(f"Anaplan client ID in use: {_mask(ANAPLAN_CLIENT_ID)}")
    if PUSH_TO_FLEXERA:
        print(f"Flexera client ID in use: {_mask(CLIENT_ID)}")


# ===========================================================================
# ANAPLAN - HTTP helpers & authentication
# ===========================================================================

def raise_for_anaplan_error(response):
    """Raise a useful exception when an Anaplan request is unsuccessful."""
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        details = response.text.strip()
        if details:
            raise requests.HTTPError(
                f"{exc}. Response body: {details}", response=response
            ) from exc
        raise

def get_device_code(client_id, scope):
    """Request a device code for interactive user authentication."""
    response = requests.post(
        OAUTH_DEVICE_CODE_URL,
        headers={"Content-Type": "application/json"},
        json={"client_id": client_id, "scope": scope},
        timeout=60,
    )
    raise_for_anaplan_error(response)
    return response.json()

def authorize_user(verification_uri_complete):
    """Show the URL that the user must open to authorize the application."""
    print("\nPlease complete the Anaplan authorization process:")
    print(f"Open this URL in your browser: {verification_uri_complete}\n")

def get_anaplan_access_token(client_id, device_code, interval=5):
    """Poll Anaplan until device-code authorization is completed."""
    payload = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
        "client_id": client_id,
    }
    while True:
        response = requests.post(
            OAUTH_TOKEN_URL,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
        if response.status_code == 200:
            return response.json()
        if response.status_code in (400, 403):
            try:
                error = response.json().get("error")
            except (ValueError, json.JSONDecodeError):
                error = None
            if error == "authorization_pending":
                print("Waiting for user authorization...")
                time.sleep(interval)
                continue
            if error == "slow_down":
                interval += 5
                time.sleep(interval)
                continue
        raise_for_anaplan_error(response)

def refresh_access_token(client_id, refresh_token):
    """Get a new access token using a saved refresh token."""
    response = requests.post(
        OAUTH_TOKEN_URL,
        headers={"Content-Type": "application/json"},
        json={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
        timeout=60,
    )
    raise_for_anaplan_error(response)
    tokens = response.json()
    tokens.setdefault("refresh_token", refresh_token)
    return tokens

def save_tokens(tokens, file_path=TOKEN_FILE):
    """Save OAuth tokens in the configured local directory."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("w", encoding="utf-8") as token_file:
        json.dump(tokens, token_file, indent=2)

def load_tokens(file_path=TOKEN_FILE):
    """Load saved OAuth tokens, or return None when unavailable or invalid."""
    try:
        with file_path.open("r", encoding="utf-8") as token_file:
            content = token_file.read().strip()
        return json.loads(content) if content else None
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def authenticate():
    """Return a valid Anaplan access token."""
    tokens = load_tokens()
    if tokens and tokens.get("refresh_token"):
        print("Using the saved refresh token...")
        try:
            tokens = refresh_access_token(ANAPLAN_CLIENT_ID, tokens["refresh_token"])
            save_tokens(tokens)
            print("Anaplan access token refreshed successfully.")
            return tokens["access_token"]
        except requests.RequestException as exc:
            print(f"Refresh failed: {exc}")
            print("Switching to device-code authorization...")

    try:
        device_response = get_device_code(ANAPLAN_CLIENT_ID, SCOPE)
    except requests.HTTPError as exc:
        if "unauthorized_client" in str(exc):
            sys.exit(
                "\nAnaplan rejected ANAPLAN_CLIENT_ID "
                f"({_mask(ANAPLAN_CLIENT_ID)}): 'Unauthorized or unknown client'.\n"
                "Fix: set ANAPLAN_CLIENT_ID to the Client ID of the Anaplan OAuth client\n"
                "(the value your original Anaplan export script used - NOT the Flexera\n"
                "Client ID). Check it in Anaplan Administration > Security > OAuth\n"
                "clients, and make sure the Device Grant flow is enabled for that client."
            )
        raise
    authorize_user(device_response["verification_uri_complete"])
    tokens = get_anaplan_access_token(
        ANAPLAN_CLIENT_ID,
        device_code=device_response["device_code"],
        interval=device_response.get("interval", 5),
    )
    save_tokens(tokens)
    print("Anaplan access token obtained successfully.")
    return tokens["access_token"]


# ===========================================================================
# ANAPLAN - CSV post-processing
# ===========================================================================

def parse_datetime_value(value):
    """Convert common Anaplan date/time strings to a datetime, or return None."""
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    common_formats = (
        "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y", "%d-%m-%Y",
        "%d %b %Y", "%d %B %Y", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
    )
    for date_format in common_formats:
        try:
            return datetime.strptime(value, date_format)
        except ValueError:
            continue
    return None

def parse_login_date(value):
    """Convert a supported date/time string to a date, or return None."""
    parsed = parse_datetime_value(value)
    return parsed.date() if parsed else None

def looks_like_date(value):
    """Return True when a non-empty value can be parsed as a date."""
    return bool(value.strip()) and parse_login_date(value) is not None

def post_process_export(output_file=OUTPUT_FILE):
    """Remove a date-only A1 row and add the 90-day inactivity column."""
    with output_file.open("r", encoding="utf-8-sig", newline="") as source:
        sample = source.read(4096)
        source.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;|\t")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(source, dialect))

    if not rows:
        raise RuntimeError("The downloaded CSV is empty.")

    # A report period such as "Aug 26" can sit alone in A1 with the real
    # field names on row 2 - detect and drop it.
    first_row_nonempty = [cell.strip() for cell in rows[0] if cell.strip()]
    second_row_headers = (
        {cell.strip().casefold() for cell in rows[1] if cell.strip()}
        if len(rows) > 1 else set()
    )
    expected_second_row_headers = {
        "user id", "account creation date", "last active date", "license state",
    }
    first_row_is_metadata = (
        len(rows) > 1
        and len(first_row_nonempty) == 1
        and (
            looks_like_date(first_row_nonempty[0])
            or len(expected_second_row_headers & second_row_headers) >= 2
        )
    )
    if first_row_is_metadata:
        removed_value = first_row_nonempty[0]
        rows.pop(0)
        print(f"Removed the A1 metadata row: {removed_value!r}.")

    headers = [header.strip() for header in rows[0]]
    if not any(headers):
        raise RuntimeError("No CSV column headings were found after removing A1.")

    if not headers[0]:
        headers[0] = "Email ID"
        print('Added missing first-column header: "Email ID".')

    header_lookup = {header.casefold(): index for index, header in enumerate(headers)}
    login_index = header_lookup.get(LAST_LOGIN_COLUMN.casefold())
    if login_index is None:
        for candidate in ("last active date", "last login", "last login date",
                          "last login time", "last login datetime", "last logged in"):
            if candidate in header_lookup:
                login_index = header_lookup[candidate]
                break
    if login_index is None:
        raise ValueError(
            f'Could not find the last-login column "{LAST_LOGIN_COLUMN}". '
            f"Available columns: {', '.join(headers)}"
        )
    login_header = headers[login_index]

    # Drop previously calculated columns so reruns don't duplicate them.
    calculated_names = {INACTIVE_DAYS_COLUMN.casefold(), INACTIVE_COLUMN.casefold()}
    keep_indexes = [i for i, h in enumerate(headers) if h.casefold() not in calculated_names]
    if len(keep_indexes) != len(headers):
        headers = [headers[i] for i in keep_indexes]
        rows = [[row[i] if i < len(row) else "" for i in keep_indexes] for row in rows]
        login_index = headers.index(login_header)

    headers.extend([INACTIVE_DAYS_COLUMN, INACTIVE_COLUMN])
    inactive_days_index = len(headers) - 2
    inactive_index = len(headers) - 1
    current_headers = {header.casefold(): index for index, header in enumerate(headers)}

    today = date.today()
    processed_rows = [headers]
    for row_number, row in enumerate(rows[1:], start=2):
        row = list(row)
        if len(row) < len(headers):
            row.extend([""] * (len(headers) - len(row)))

        # Normalise date columns to ISO 8601 UTC, e.g. 2026-05-06T21:58:12Z
        for date_column in ISO_DATETIME_COLUMNS:
            date_index = current_headers.get(date_column.casefold())
            if date_index is None:
                raise ValueError(
                    f'Could not find required date column "{date_column}". '
                    f"Available columns: {', '.join(headers)}"
                )
            raw_date = row[date_index].strip() if date_index < len(row) else ""
            if raw_date:
                normalized = parse_datetime_value(raw_date)
                if normalized is None:
                    print(f"Warning: unrecognized {date_column} on CSV row "
                          f"{row_number}: {raw_date!r}")
                else:
                    if normalized.tzinfo is None:
                        normalized = normalized.replace(tzinfo=timezone.utc)
                    else:
                        normalized = normalized.astimezone(timezone.utc)
                    row[date_index] = normalized.isoformat(
                        timespec="seconds").replace("+00:00", "Z")

        raw_login = row[login_index] if login_index < len(row) else ""
        login_date = parse_login_date(raw_login)
        if not raw_login.strip():
            inactive_days, status = "", "Unknown"
        elif login_date is None:
            inactive_days, status = "", "Invalid date"
            print(f"Warning: unrecognized last-login date on CSV row {row_number}: {raw_login!r}")
        else:
            inactive_days = max(0, (today - login_date).days)
            status = "Yes" if inactive_days > INACTIVE_DAYS else "No"
        row[inactive_days_index] = inactive_days
        row[inactive_index] = status
        processed_rows.append(row)

    temporary_file = output_file.with_suffix(output_file.suffix + ".processed")
    with temporary_file.open("w", encoding="utf-8-sig", newline="") as destination:
        csv.writer(destination, dialect).writerows(processed_rows)
    temporary_file.replace(output_file)
    print(f'Added columns "{INACTIVE_DAYS_COLUMN}" and "{INACTIVE_COLUMN}" '
          f"using a {INACTIVE_DAYS}-day threshold.")


# ===========================================================================
# ANAPLAN - export & download
# ===========================================================================

def start_export(headers):
    """Start the configured Anaplan export and return its task ID."""
    url = (f"{ANAPLAN_API_BASE_URL}/workspaces/{WORKSPACE_ID}"
           f"/models/{MODEL_ID}/exports/{EXPORT_ID}/tasks")
    response = requests.post(url, headers=headers, json={"localeName": "en_US"}, timeout=60)
    raise_for_anaplan_error(response)
    task_id = response.json()["task"]["taskId"]
    print(f"Export started. Task ID: {task_id}")
    return task_id

def wait_for_export(headers, task_id, polling_interval=5):
    """Wait until the export task completes or fails."""
    url = (f"{ANAPLAN_API_BASE_URL}/workspaces/{WORKSPACE_ID}"
           f"/models/{MODEL_ID}/exports/{EXPORT_ID}/tasks/{task_id}")
    while True:
        response = requests.get(url, headers=headers, timeout=60)
        raise_for_anaplan_error(response)
        task = response.json()["task"]
        status = task["taskState"]
        print(f"Export status: {status}")
        if status == "COMPLETE":
            return
        if status in {"FAILED", "CANCELLED"}:
            raise RuntimeError(f"Anaplan export did not complete: {status}. {task.get('result', {})}")
        time.sleep(polling_interval)

def get_all_chunks(headers):
    """Retrieve the full list of chunks for the completed export file."""
    url = (f"{ANAPLAN_API_BASE_URL}/workspaces/{WORKSPACE_ID}"
           f"/models/{MODEL_ID}/files/{EXPORT_ID}/chunks")
    response = requests.get(url, headers=headers, timeout=60)
    raise_for_anaplan_error(response)
    return url, response.json().get("chunks", [])

def download_entire_dump(headers, output_file=OUTPUT_FILE):
    """Download every returned chunk and combine them into one output file."""
    chunks_url, chunks = get_all_chunks(headers)
    if not chunks:
        raise RuntimeError("The completed export returned no downloadable chunks.")

    output_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = output_file.with_suffix(output_file.suffix + ".part")
    try:
        with temporary_file.open("wb") as destination:
            total_chunks = len(chunks)
            for position, chunk in enumerate(chunks, start=1):
                chunk_id = str(chunk["id"])
                print(f"Downloading chunk {position}/{total_chunks}: {chunk_id}")
                chunk_headers = {
                    "Authorization": headers["Authorization"],
                    "Accept": "application/octet-stream",
                }
                with requests.get(f"{chunks_url}/{chunk_id}", headers=chunk_headers,
                                  timeout=300, stream=True) as response:
                    raise_for_anaplan_error(response)
                    for block in response.iter_content(chunk_size=1024 * 1024):
                        if block:
                            destination.write(block)
        temporary_file.replace(output_file)
    except Exception:
        temporary_file.unlink(missing_ok=True)
        raise
    print(f"Entire export dump downloaded to: {output_file}")
    print(f"Downloaded file size: {output_file.stat().st_size:,} bytes")

def run_anaplan_export():
    """Authenticate, run the export, download it and post-process it."""
    access_token = authenticate()
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    task_id = start_export(headers)
    wait_for_export(headers, task_id)
    download_entire_dump(headers)
    post_process_export(OUTPUT_FILE)


# ===========================================================================
# FLEXERA API (unchanged)
# ===========================================================================

def get_access_token() -> str:
    if not CLIENT_ID or not CLIENT_SECRET:
        sys.exit("FLEXERA_CLIENT_ID / FLEXERA_CLIENT_SECRET are not set.")
    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials",
              "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    print("Obtained access token.")
    return resp.json()["access_token"]

def request_upload_url(token: str) -> tuple[str, str]:
    resp = requests.post(UPLOAD_URL,
                         headers={"Authorization": f"Bearer {token}"}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    print("Received upload URL (fileId=%s, expires in %ss).", data.get("fileId"), data.get("expires"))
    return data["url"], data["fileId"]

def upload_csv_to_blob(presigned_url: str, csv_path: str) -> None:
    with open(csv_path, "rb") as f:
        resp = requests.put(
            presigned_url, data=f,
            headers={"x-ms-blob-type": "BlockBlob", "Content-Type": "text/csv"},
            timeout=120,
        )
    resp.raise_for_status()
    print("Uploaded CSV to blob storage (HTTP %s).", resp.status_code)

def trigger_import(token: str, file_id: str, config_id: str) -> None:
    IMPORT_URL = f"{BASE}/api/saas/import/v1/configs/{config_id}/records/import"
    resp = requests.post(
        IMPORT_URL,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        data=json.dumps({"fileId": file_id}),
        timeout=60,
    )
    resp.raise_for_status()
    print("Import task created (HTTP %s). Response: %s", resp.status_code, resp.text[:500])

def make_user_id(email: str, org: str) -> str:
    key = f"{org.strip().lower()}\x00{email.strip().lower()}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


# ===========================================================================
# BUILD FLEXERA FILES
# ===========================================================================

def _norm(s) -> str:
    return " ".join(str(s).strip().lower().split())

def load_export_for_flexera() -> pd.DataFrame:
    """Read the post-processed Anaplan export (delimiter auto-detected)."""
    if not OUTPUT_FILE.exists():
        sys.exit(f"Anaplan export not found: {OUTPUT_FILE}")
    df = pd.read_csv(OUTPUT_FILE, sep=None, engine="python", dtype=str,
                     keep_default_na=False, encoding="utf-8-sig")
    print(f"Loaded {len(df)} rows from {OUTPUT_FILE.name}.")

    rename, actual = {}, {_norm(c): c for c in df.columns}
    for expected, internal in SOURCE_COLUMNS.items():
        exp = _norm(expected)
        match = actual.get(exp) or next(
            (orig for a, orig in actual.items() if a.startswith(exp) and orig not in rename), None)
        if match is not None:
            rename[match] = internal
    df = df.rename(columns=rename)
    missing = [c for c in REQUIRED_SOURCE if c not in df.columns]
    if missing:
        sys.exit(f"Anaplan export is missing required column(s): {missing}. Found: {list(df.columns)}")
    return df

def _zone_key(value) -> str:
    """Uppercase, punctuation -> space, collapsed spaces."""
    cleaned = "".join(ch if ch.isalnum() else " " for ch in str(value).upper())
    return " ".join(cleaned.split())

# Longest aliases first so "SOUTH AMERICA ZONE" wins over "SOUTH AMERICA"
_ZONE_ALIAS_ORDER = sorted(ZONE_ALIASES, key=lambda a: (-len(a.split()), -len(a)))

def normalize_zone(value) -> tuple:
    """Return (canonical_zone, recognised: bool)."""
    key = _zone_key(value)
    if key in BLANK_ZONE_VALUES:
        return DEFAULT_ZONE, True
    padded = f" {key} "
    compact = key.replace(" ", "")
    # 1) whole-word alias match ("GHQ SOLUTIONS", "North America Zone")
    for alias in _ZONE_ALIAS_ORDER:
        if f" {alias} " in padded:
            return ZONE_ALIASES[alias], True
    # 2) run-together spellings ("NorthAmerica", "SOUTHAMERICAZONE")
    for alias in _ZONE_ALIAS_ORDER:
        alias_compact = alias.replace(" ", "")
        if len(alias_compact) >= 4 and alias_compact in compact:
            return ZONE_ALIASES[alias], True
    return UNMAPPED_ZONE_DEFAULT, False

def apply_zone_normalisation(raw: pd.DataFrame) -> pd.Series:
    """Normalised zone per row, with a summary and a warning for unknowns."""
    source = raw["zone"] if "zone" in raw.columns else pd.Series("", index=raw.index)
    results = source.map(normalize_zone)
    zones = results.map(lambda r: r[0])
    unmapped = source[~results.map(lambda r: r[1])]
    counts = zones.value_counts()
    print("Zone mapping: " + ", ".join(f"{z}={counts.get(z, 0)}" for z in CANONICAL_ZONES))
    if len(unmapped):
        print(f"Warning: {len(unmapped)} row(s) with unrecognised Assigned Zone set to "
              f"{UNMAPPED_ZONE_DEFAULT} - add them to ZONE_ALIASES: "
              f"{sorted(unmapped.str.strip().unique().tolist())}")
    return zones

def build_flexera_frames(raw: pd.DataFrame):
    """Return (assignments, activities) with Flexera headers, dates untouched."""
    raw = raw.copy()
    raw["email"] = raw["email"].str.strip().str.lower()
    raw = raw[raw["email"] != ""]

    if ONLY_ENABLED_USERS and "user_state" in raw.columns:
        raw = raw[raw["user_state"].str.strip().str.lower() == "enabled"]

    ids = raw["anaplan_id"].str.strip() if "anaplan_id" in raw.columns else pd.Series("", index=raw.index)
    raw["user_id"] = [aid if aid else make_user_id(email, ANAPLAN_TENANT_KEY)
                      for aid, email in zip(ids, raw["email"])]
    raw["zone_norm"] = apply_zone_normalisation(raw)
    raw["license_final"] = [
        f"{lt.strip()} {zone}".strip()
        for lt, zone in zip(raw["license_type"], raw["zone_norm"])
    ]
    created = raw["created"].str.strip() if "created" in raw.columns else ""
    last_active = raw["last_active"].str.strip()

    assignments = pd.DataFrame({
        "Email ID":              raw["email"],
        "User ID":               raw["user_id"],
        "License Type":          raw["license_final"],
        "Account Creation Date": created,
        "Last Active Date":      last_active,
    })[ASSIGNMENTS_TEMPLATE]
    assignments = assignments.drop_duplicates(subset=["Email ID", "License Type"]).reset_index(drop=True)

    activities = pd.DataFrame({"User ID": raw["user_id"], "Last Active Date": last_active})
    activities = activities[activities["Last Active Date"] != ""]
    activities = (activities
                  .assign(_sort=pd.to_datetime(activities["Last Active Date"], errors="coerce", utc=True))
                  .sort_values("_sort", ascending=False, na_position="last")
                  .drop_duplicates(subset=["User ID"], keep="first")
                  .drop(columns="_sort"))[ACTIVITIES_TEMPLATE].reset_index(drop=True)
    return assignments, activities

def push_to_flexera(csv_path: Path, config_id: str, label: str) -> None:
    print(f"\n--- Pushing {label} ({csv_path.name}) ---")
    access_token = get_access_token()
    presigned_url, file_id = request_upload_url(access_token)
    upload_csv_to_blob(presigned_url, str(csv_path))
    trigger_import(access_token, file_id, config_id=config_id)


# ===========================================================================
# MAIN
# ===========================================================================

def main():
    run_date = date.today().strftime("%Y-%m-%d")
    print(f"=== Anaplan -> Flexera sync | {run_date} ===")
    BASE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    validate_config()

    if RUN_ANAPLAN_EXPORT:
        run_anaplan_export()
    else:
        print(f"RUN_ANAPLAN_EXPORT is False - using existing {OUTPUT_FILE.name}.")

    assignments, activities = build_flexera_frames(load_export_for_flexera())

    # Flexera CSVs: fixed names, plain UTF-8, comma-delimited
    assignments.to_csv(ASSIGNMENTS_CSV, index=False, encoding="utf-8")
    activities.to_csv(ACTIVITIES_CSV, index=False, encoding="utf-8")
    print(f"Wrote {len(assignments)} assignment rows -> {ASSIGNMENTS_CSV.name} {list(assignments.columns)}")
    print(f"Wrote {len(activities)} activity rows   -> {ACTIVITIES_CSV.name} {list(activities.columns)}")

    if not PUSH_TO_FLEXERA:
        print("PUSH_TO_FLEXERA is False - files built, nothing sent.")
        return

    if len(assignments):
        push_to_flexera(ASSIGNMENTS_CSV, ASSIGNMENTS_CONFIG_ID, "user assignments")
    if len(activities):
        push_to_flexera(ACTIVITIES_CSV, ACTIVITIES_CONFIG_ID, "user activities")
    print("\nDone.")

if __name__ == "__main__":
    main()
