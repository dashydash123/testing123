# Anaplan ELP — API Source Mapping for User & Entitlement Attributes

**Context:** Anaplan is the *licensed product* being measured. Objective is an Effective Licence Position for the Anaplan tenant.
**Sourced from:** `anaplan.docs.apiary.io` (Integration API v2 reference) and `help.anaplan.com/anaplan-api-844c6d40-a21c-423d-8435-ebaaa0372b76` (API index), plus the Anapedia pages those index.
**Date:** August 2026

---

## 1. Correction to my earlier note

The API index page lists **eight** API surfaces, not the subset I described previously. Two of them matter enormously for ELP and I under-weighted them:

| API | Relevance to ELP |
|---|---|
| Integration API v2.0 | Workspace/model metadata, workspace-scoped user lists, capacity |
| **Administration API** | **Bulk user import *and export* endpoints — carries user details AND licence information. This is the primary ELP source.** |
| **Audit API** | **The only source of usage/activity events. Base: `https://audit.anaplan.com/audit/api/1/`** |
| SCIM API | User object with `meta.created`, `active`, `externalId`. Enterprise tier + SSO prerequisite |
| Exception users API | Exception-user flag per workspace |
| Authentication service API | Token issuance |
| Financial Consolidation API | Has its own user-management/security endpoints (user ID, name, enabled/disabled, email, roles) — only relevant if the client uses FC |
| CloudWorks API / ALM API | Not ELP-relevant |

**The Administration API is the one to anchor on.** Anapedia states repeatedly that user administrators use its bulk user import and export endpoints to create users and update user details *and licence information*. That is the licence-type field you need.

⚠️ Anapedia now describes these APIs at summary level and links out to Postman collections rather than publishing endpoint paths inline. **Pull the official Anaplan Postman collection and read the Administration folder to get the exact export endpoint path, query parameters and response schema.** I could not verify the literal URI from public documentation, and you should not put an unverified path into a client deliverable.

---

## 2. Field-by-field mapping

Legend: ✅ available directly · 🔶 derivable from Anaplan data · ❌ not in Anaplan — external source or your own rule

| # | Field | Status | Source | Notes |
|---|---|---|---|---|
| 1 | **User ID** | ✅ | Administration API bulk export; `GET /2/0/workspaces/{wsId}/users` → `id`; SCIM `GET /scim/1/0/v2/Users` → `id`; also the `userId` attribute on every Audit event | Visible in UI at Administration → Users → Internal → Details tab. This is your join key across all three surfaces |
| 2 | **Leavers Mapping** | ❌ | HRIS / IdP — not Anaplan | See §3. This is the single highest-value field in the whole ELP |
| 3 | **License Type** | ✅ | **Administration API bulk user export only** | Not exposed by Integration API v2 `/users` or by SCIM. Anaplan licence types are named tiers with allocated seat counts (e.g. Planner; Participant, which additionally requires one or more Lines of Business to be selected) |
| 4 | **User State** | ✅ | Administration API export; SCIM `active` boolean; "Enabled" column on the Internal page | For change history use Audit `USR-3` (user enabled) / `USR-4` (user disabled) |
| 5 | **User Type** | ✅ | Administration API export; Administration console separates Users → **Internal** and Users → **Visiting** | Add the exception-user flag as a third dimension via the Exception users API. Visiting users are external/partner accounts that still consume seats |
| 6 | **License State** | ⚠️ | Depends on your definition — see §4 | Ambiguous as written. Clarify before building |
| 7 | **Assigned Zone** | ❌ | Not an Anaplan concept | See §4. Anaplan has no per-user "zone" attribute |
| 8 | **Highest Active Workspace** | 🔶 | `GET /2/0/workspaces?tenantDetails=true` (all workspaces + capacity) + user→workspace assignments from the Administration API export / SCIM Groups | "Highest" requires a ranking rule you define — prod > UAT > sandbox, or by workspace tier. Compute in the ELP model, not in the extract |
| 9 | **Assignment Criteria** | ❌ | Your own entitlement policy | e.g. "Planner if WSA role OR executed an action in last 90 days; Participant otherwise." This is the rule the ELP *tests*, not a field Anaplan supplies |
| 10 | **Account Creation Date** | ✅ | SCIM `meta.created` (ISO 8601) | Audit `USR-1` (User created) also carries it, but only inside the retention window — SCIM is the reliable source for users created before that. If SCIM isn't licensed, request a one-off export from Anaplan support |
| 11 | **Last Active Date** | ✅ | "Last login time" column on the Internal page / Administration API export; Audit `USR-8` (login success) | **Last *login* ≠ last *activity*.** A user who logs in daily and does nothing looks active. Use #12 for real usage |
| 12 | **Last Model Activity** | ✅ | **Audit API only** — `USR-13` (user access to model, success), `objectId` = model | Also `USR-19` dashboard accessed, `USR-20` executed action, `USR-43/44/45` UX board/worksheet/report page opened. Retention-constrained — see §5 |
| 13 | **Model Access Count** | 🔶 | Count of `USR-13` per `userId` per period, from the Audit API | Same retention constraint. Decide whether "access" means model-open only, or model-open + dashboard + action + UX page |
| 14 | **WS Access Count** | 🔶 | **No workspace-open event exists.** Derive: `USR-13` model IDs → `GET /2/0/models` → `currentWorkspaceId` → count distinct workspaces per user per period | Be explicit in the deliverable that this is derived, not observed |
| 15 | **Zone Access Count** | ❌ | Undefined until #7 is defined | Blocked on the same clarification |

---

## 3. Leavers mapping — the important one

Anaplan holds no employment status. The join has to come from HR or the IdP. Two things make this the highest-value field in the ELP:

**Disabling a user does not release the licence seat.** A disabled Anaplan account retains its workspace assignments and continues to consume a seat. Only removal frees it. So the population of *disabled-but-licensed* accounts is pure reclaim, and it is invisible unless you join to leaver data.

**Use `externalId`, not email.** The SCIM user object has an `externalId` field explicitly intended to correlate Anaplan users to IdP records (Okta / Entra ID object ID). If the client provisions via SCIM, this field is already populated and the leaver join is deterministic. If they don't, you're string-matching on `userName` (email) — which breaks on name changes, domain migrations, and contractor accounts.

Worth flagging as a recommendation regardless of the ELP outcome: Anapedia advises against reusing the email addresses of removed users, so email is not a stable key over time.

Recommended leaver categories for the ELP:
- Terminated in HR, still enabled in Anaplan → **immediate revoke** (also a security finding, not just a cost one)
- Terminated in HR, disabled in Anaplan, still holding a seat → **reclaim**
- Active in HR, no model activity in N days → **downgrade candidate**
- Not in HR at all → orphan / service account / visiting user → **classify and own**

---

## 4. Two fields you need to clarify before building

**"License State"** could mean either of two very different things:
- *Assignment state* — assigned vs auto-assigned. Anaplan auto-assigns the lowest-level licence per the contract to any internal or visiting user without one, and explicitly states there is no penalty if that pushes you over the limit. Auto-assigned users are a distinct population worth flagging.
- *Consumption state* — entitled vs consumed at tenant level. That's the Administration → Summary page, which shows seats assigned against seats purchased per licence type (Anapedia's own example shows 73 of 70 Planner seats assigned — i.e. **the platform lets you go over and tells you about it**). That's your compliance gap.

Get the definition from whoever owns the ELP template. They are different columns.

**"Assigned Zone"** has no Anaplan equivalent. The three plausible intents:
- *Tenant region/instance* — visible in the login URL (`us1a`, `eu1a`, etc.). One value for the whole tenant, so useless as a per-user field.
- *Business region for chargeback* — an HR/IdP attribute, joined in.
- *A field carried over from another vendor's ELP template.* This is my guess — the field list reads like a generic SaaS ELP schema.

Until that's resolved, #7 and #15 stay unmapped.

---

## 5. The Audit API retention constraint — design around this first

**Anaplan Audit provides 30 or 90 days of logs depending on your edition.** Anaplan states plainly that Audit is a delivery mechanism, not a permanent repository, and that customers are expected to pull frequently and store logs themselves.

Consequences for the ELP:

1. **Fields #12, #13 and #14 cannot be produced retrospectively.** If the client wants "no activity in 12 months" as a reclaim criterion, you must start warehousing audit events now and wait. There is no back-fill.
2. **Stand up the audit pull as a separate, earlier workstream** than the ELP build. Incremental extraction keyed on last-run epoch, landed into a database you control. There is a well-known community reference implementation for exactly this pattern.
3. **Access requirements:** Audit must be *enabled* for the tenant, and the calling account needs the **Tenant Auditor** role. Tenant auditors have no permissions at all until Audit is enabled — so confirm both. Audit is an Enterprise/Professional-edition capability.
4. **No webhooks.** Anaplan has no outbound push for user lifecycle or activity events. Polling is the only option.

---

## 6. Access the service account needs

| Requirement | Why |
|---|---|
| **User Administrator** role | Administration API bulk user export — licence type, user state, user type |
| **Tenant Auditor** role | Audit API — all activity fields |
| Audit **enabled** on the tenant | The role is inert without it |
| Workspace access (or Workspace Administrator) | Integration API v2 workspace/model metadata and workspace-scoped user lists. A non-WSA can only retrieve their own user record from `/workspaces/{id}/users` |
| SCIM API key (separate credential) | Only if using SCIM for `meta.created` / `externalId`. Enterprise tier, SSO prerequisite |
| OAuth client or CA certificate | Auth. Device grant is unavailable if the tenant uses SSO — likely here, since SSO is a SCIM prerequisite. Plan for certificate auth |

Note the SCIM credential is a **dedicated API key**, distinct from the AnaplanAuthToken used everywhere else. And API key rotation is not natively supported — the only path is revoke, delete, recreate, which means downtime for anything depending on it. Factor that into the rotation policy.

---

## 7. Operational constraints

- **Rate limiting is tenant-level**, shared across all workspaces. A 429 returns a `Retry-After` header; Anaplan's own guidance is to hard-code a 10-second timeout on 429. Community reports put the ceiling around 600 requests/minute, but this is not officially published — treat it as indicative and back off exponentially.
- **Case sensitivity:** workspace IDs lowercase, model IDs UPPERCASE. Wrong case = object not recognised, not an obvious error.
- **Pagination** is offset-based on the Integration API v2 (`limit`, `offset`, sort attribute).
- **TLS 1.3 required.**
- Anaplan's published policy explicitly restricts MCP servers and certain AI services from using its APIs. If any part of the delivery approach involves an AI agent calling Anaplan, surface it before design sign-off.

---

## 8. Suggested extract set

Five pulls, joined on user ID:

1. **Administration API — bulk user export** → user ID, name, email, licence type, user state, user type, workspace assignments *(exact endpoint: confirm from the Postman collection)*
2. **`GET /2/0/workspaces?tenantDetails=true`** → workspace ID, name, active, `sizeAllowance`, `currentSize` (bytes) — the capacity entitlement dimension, since Anaplan licenses workspace capacity as well as seats
3. **`GET /2/0/models`** → model ID, name, `activeState`, `currentWorkspaceId` — needed to roll model events up to workspace for field #14
4. **Audit API, incremental** → `USR-8`, `USR-13`, `USR-19`, `USR-20`, `USR-43/44/45` — everything in fields #11–14
5. **SCIM `GET /Users`** *(if licensed)* → `meta.created`, `externalId`, `active`

Plus, from outside Anaplan:
6. **HRIS/IdP leaver feed** → joined on `externalId`, falling back to email
7. **Contract / order form** → purchased seats per licence type. ⚠️ **VERIFY** whether the Administration API exposes tenant-level entitlement counts, or whether the Summary page figures are UI-only. If UI-only, the entitled side of the ELP comes from the contract and has to be maintained manually — that is a material finding for the design.

---

## 9. Open items

1. Exact Administration API bulk-export endpoint path and response schema — read from the official Anaplan Postman collection.
2. Definition of "License State" (§4).
3. Definition of "Assigned Zone" (§4) — blocks fields #7 and #15.
4. Client's Anaplan edition — determines Audit retention (30 vs 90 days) and SCIM availability.
5. Whether tenant-level purchased-seat counts are API-accessible or contract-only (§8, item 7).
6. Is SSO enabled? Determines auth method and whether SCIM is even possible.
7. Definition of "access" for the count fields — model-open only, or all interaction event types.
