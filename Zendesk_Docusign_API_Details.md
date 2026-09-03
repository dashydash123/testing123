GET /api/v2/custom_roles — resolves custom role names (always called)

GET /api/v2/incremental/users/cursor — fetches users for a date window (--start/--last-days)

GET /api/v2/users — fetches full roster, server-side role filtering (--all-time)

Flow:

/custom_roles runs first, every time — builds a lookup table of role ID → role name (e.g. "Lider de equipo").
Then either:
/incremental/users/cursor — if you gave a date window, pulls users in timestamp-ordered pages using a cursor token, then the script filters locally to your date range.
/users — if you passed --all-time, pulls the full roster instead, with Zendesk filtering out end-users server-side via role[]=agent&role[]=admin.
The script dedupes by user ID, cross-references each user's role_type/custom_role_id against the table from step 1 to get a real seat_class, then writes it all to CSV.


Four endpoints:

1. POST https://account.docusign.com/oauth/token
Auth server. Basic auth with integration key + secret, grant_type=refresh_token. Only called if DS_ACCESS_TOKEN is unset. Rotates the refresh token.

2. GET https://account.docusign.com/oauth/userinfo
Resolves base_uri (e.g. https://na3.docusign.net) and the account ID/name. Everything below is built off that.

3. GET {base_uri}/restapi/v2.1/accounts/{accountId}/users
Users:list. Paginated via start_position / count. Optional additional_info=true for lastLogin and createdDateTime.

4. GET {base_uri}/restapi/v2.1/accounts/{accountId}/envelopes
Envelopes:listStatusChanges. The main one. Params used: from_date, to_date, from_to_status=created, include=recipients,custom_fields, count=100, start_position, order_by=created, order=asc

The script runs a straight five-step pipeline, no state between runs:

1. Get a token. If DS_ACCESS_TOKEN is set, it uses it as-is. Otherwise it POSTs to /oauth/token with the integration key + secret as Basic auth and your refresh token, and writes the new pair to docusign_tokens.txt.

2. Resolve where to call. GET /oauth/userinfo returns the accounts the token can see. It picks yours (or the default) and stitches together {base_uri}/restapi/v2.1/accounts/{accountId} — every later call hangs off that string.

3. Pull users. Loops GET /users with start_position advancing by batch size until start_position >= totalSetSize. Builds two lookup maps: userId → row, and lowercased email → userId.

4. Pull envelopes. Splits your date range into 30-day windows, then for each window pages GET /envelopes 100 at a time with from_to_status=created and include=recipients. Everything accumulates into one flat in-memory list.

5. Join in Python, not in DocuSign. There's no API that gives per-user envelope counts, so the script does the attribution itself. For each envelope it matches envelope.sender against the user maps — userId first, email as fallback — and increments that user's sent counters. Then it walks every recipient block (signers, carbon copies, agents, etc.), dedupes so one person on an envelope twice only counts once, and increments their recipient counters. Senders it can't match go to unmatched_senders.

Then it writes three CSVs plus the raw JSON.

The whole design assumption is that both list endpoints tolerate being read in full and the correlation happens client-side. That's also where your accuracy risk lives — the join quality depends entirely on userId/email matching, and the counts depend on from_to_status=created meaning the same thing DocuSign's dashboard means.