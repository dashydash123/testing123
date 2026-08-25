# Fixing DocuSign Envelope Usage and Zendesk Role Data in Flexera One
*Root cause, required APIs, and the ingestion path*

---

## 1. DocuSign — per-user envelope count

### 1.1 Root cause

The Flexera Docusign connector makes exactly five calls. None of them is the envelope endpoint. [1]

| Connector call | Returns | Level |
|---|---|---|
| `POST /oauth/token` | Access + refresh token | — |
| `GET /oauth/userinfo` | Accounts on the token | Account |
| `GET /restapi/v2.1/accounts/{id}/users` | User roster | User |
| `GET /restapi/v2.1/accounts/{id}/billing_plan` | Licence details | Account |
| `GET /restapi/v2.1/accounts/{id}/billing_charges` | Envelope usage | Account |

> **This is the answer:** users are captured because the connector calls `/users`. Envelope usage is captured only from `/billing_charges`, which is an account-level aggregate. The connector never retrieves individual envelopes, so no per-user envelope attribution exists in Flexera at all. Any per-user figure you see is either blank or apportioned, not measured — there is nothing to tune in the connector.

### 1.2 The API that gives per-user counts

Envelopes: listStatusChanges is the only source of envelope-level data with a sender attached. [2][3]

```
GET {base_uri}/restapi/v2.1/accounts/{accountId}/envelopes
    ?from_date=2026-04-01T00:00:00Z&to_date=2026-06-30T23:59:59Z
    &status=any&from_to_status=any&count=100&start_position=0
Authorization: Bearer {access_token}
```

Aggregate the response on each envelope's sender object (`sender.userId`, `sender.email`). Join that `userId` back to the `/users` roster — the same key the connector already uses — to produce envelopes per user.

### 1.3 Four things that will corrupt the count

| Trap | What to do |
|---|---|
| The `user_id` query parameter | Do **NOT** use it for sender attribution. Docusign searches envelopes where that user is a recipient OR a sender, so counts inflate wherever people sign each other's documents. [3] Aggregate on the sender object instead. |
| Extraneous results | A `from_date`/`to_date` query with a `from_to_status` qualifier can return envelopes that did not meet that qualifier in the period. [2] Post-filter on `sentDateTime` inside your window. |
| Silent truncation | Max 1,000 envelopes per call and 3,000 API calls/hour per account. Page with `start_position` and compare rows retrieved against `totalSetSize` before publishing a number. |
| Multiple accounts and sites | Envelopes cannot be read across Docusign sites (NA, EU, CA) in one call. Enumerate every account from `/oauth/userinfo` and pull each separately. |

*Service accounts and bulk-send identities concentrate sends against one userId. Segregate these before reporting, or genuine users will appear dormant.*

### 1.4 Getting it into Flexera One

Per-user envelope usage is consumption data, so it goes in through ingestion, not the connector:

1. Run the envelope pull on a schedule and output a per-user usage file (user email / Docusign userId / envelopes sent / period).
2. Load it with the FSM Data Ingestion Utility, mapping your fields to Flexera's SaaS Import Job API. The utility handles CSV or API sources and scheduled uploads. [4]
3. On the application, enable the Product Consumption integration task and complete the matching CSV/API job in the utility. [5]

*Keep the connector enabled — it still supplies the roster and licence entitlement correctly. You are only supplementing the usage dimension it cannot produce.*

---

## 2. Zendesk — role-level licences

### 2.1 Root cause

The Flexera Zendesk connector makes a single call. [6]

```
GET https://{subdomain}.zendesk.com/api/v2/users
```

That response carries `role` (end-user / agent / admin), `role_type` and `custom_role_id` — but `custom_role_id` is a bare integer. [7] Without a second call to resolve it, every custom role collapses to "agent", which is why you get one aggregated total instead of a per-role breakdown.

### 2.2 The two calls you need

| Call | Purpose |
|---|---|
| `GET /api/v2/custom_roles` | Role definitions: id, name, role_type, team_member_count, configuration. Enterprise / Enterprise Plus only. |
| `GET /api/v2/users?role[]=agent&role[]=admin` | Team members only. Unfiltered, this also returns every end user (customers) and pollutes the roster. |

Join `users.custom_role_id` to `custom_roles.id`. Authenticate with Basic auth using `{email}/token:{api_token}`. Use cursor pagination (`page[size]=100`) — offset pagination caps at 10,000 records.

### 2.3 Billability by role_type

| role_type | Role | Seat treatment |
|---|---|---|
| 0 | Custom agent | Paid agent seat |
| **1** | **Light agent** | **EXCLUDE — does not consume a paid agent seat** |
| 2 | Chat agent | Paid (Chat entitlement) |
| **3** | **Contributor** | **EXCLUDE — "Contributors don't occupy an agent seat in Support unless manually upgraded" [8]** |
| 4 | Admin | Paid agent seat |
| 5 | Billing admin | Paid agent seat |

*"Collaborator" is Zendesk's Collaboration add-on wording; in the API the free seats surface as role_type 1 (Light agent) and role_type 3 (Contributor). Confirm which of these the client's contract actually covers before excluding them. [8]*

> **Do not exclude on role_type alone.** A free Support seat can still consume a paid seat through an adjacent product role. Zendesk states the Knowledge Agent role consumes an extra seat when combined with the Contributor seat, and Explore Viewer / Talk Admin do not consume a seat only when paired with a free Support seat. [8] Pull the Knowledge, Analytics, Talk and Chat roles as well before finalising the exclusion list, or you will under-count.

### 2.4 AI licences

AI agents roles (Client admin, Client editor, Client user) live in the AI agents workspace and are not exposed on `/api/v2/users`. [8] I could not verify a public REST endpoint that enumerates AI agent seat assignment or Advanced AI add-on entitlement per user. Treat this as an open item: confirm with Zendesk support or the account team whether an API exists on the client's plan, and until then take AI seat counts from the Admin Center subscription page as a manual input.

### 2.5 Getting role-level licences into Flexera One

Two mechanisms matter, and they work together:

1. Push the resolved role as a per-user License Type via the FSM Data Ingestion Utility / SaaS Import Job API. [4] Without this, Flexera has no role dimension to differentiate on.
2. On the application's Licenses tab, use Create Customized License to define one licence per billable role, then set Provisioned and Cost per licence. [9]
3. Use Purchase Allocation (Licenses tab > Purchases > Edit Purchase Allocation) to allocate each purchase to only the roles you pay for. Conditions are built from Condition / Type / Value dimensions drawn from the Users tab, with operators including equals, not_equals and in / not_in. [9]
4. Set Fallback Cost on the License Details slideout to control how users excluded by those rules are costed — set it deliberately, since it still contributes to annual spend. [9]

**This is what stops the aggregation: Purchase Allocation is the supported way to say "these roles consume this paid purchase and these do not."**

> **Verify before designing.** License Differentiation is not available for every application — the Activity tab only appears for applications that support it, and the Application Task Tracking chart is the authoritative list. [10] That chart is tenant-facing and I could not retrieve it; check whether Zendesk carries License Differentiation in the client's own Flexera One before committing to this approach. If it does not, the ingestion route in step 1 plus Purchase Allocation is the fallback.

---

## 3. Summary

| | Why it is wrong | Fix |
|---|---|---|
| DocuSign envelopes | Connector reads account-level `/billing_charges` only; no envelope-level call exists | Pull `/envelopes`, aggregate on sender, ingest as consumption |
| Zendesk roles | Connector reads `/api/v2/users` only; `custom_role_id` never resolved | Add `/custom_roles`, join, ingest role as License Type |
| Paying for free seats | No role dimension, so all seats counted alike | Custom licence per role + Purchase Allocation rules |

---

## References

[1] Flexera — API calls for Docusign — https://docs.flexera.com/snow-atlas/saas/saas-connectors/prepare-docusign-connector/api-calls-for-docusign

[2] DocuSign — Envelopes: listStatusChanges — https://developers.docusign.com/docs/esign-rest-api/reference/envelopes/envelopes/liststatuschanges/

[3] DocuSign — Searching for envelopes — https://developers.docusign.com/docs/esign-rest-api/esign101/concepts/envelopes/search/

[4] Flexera — SaaS Management Data Ingestion Utility — https://docs.flexera.com/flexera/EN/SaaSManager/FSMDIU.htm

[5] Flexera — Adding an Application (Product Consumption task) — https://docs.flexera.com/flexera/EN/SaaSManager/AddApp.htm

[6] Flexera — API calls for Zendesk — https://docs.flexera.com/flexera-one/saas/flexera-one-saas-management/flexera-one-saas-management-settings/saas-connectors/prepare-zendesk-connector/api-calls-for-zendesk

[7] Zendesk — New custom role types for administrators (role_type on /users) — https://support.zendesk.com/hc/en-us/articles/4414044193306-Introducing-new-custom-role-types-for-administrators

[8] Zendesk — About team member product roles and access — https://support.zendesk.com/hc/en-us/articles/4408832171034-About-team-member-product-roles-and-access

[9] Flexera — Manually Entering License Information (custom licence, purchase allocation, fallback cost) — https://docs.flexera.com/flexera/EN/SaaSManager/ManualLicenseInfoEntry.htm

[10] Flexera — Activity Tab / License Differentiation availability — https://docs.flexera.com/flexera-one/saas/saas-manager/saas-management-user-interface-reference/saas-application-details/activity-tab

[11] Zendesk — Custom Agent Roles API reference — https://developer.zendesk.com/api-reference/ticketing/account-configuration/custom_roles/

---

*Every endpoint, parameter and seat rule above was checked against the linked vendor documentation. Two items are explicitly unverified and flagged in the text: whether a public API exposes Zendesk AI agent seat assignment (2.4), and whether Zendesk supports License Differentiation in the client's Flexera One tenant (2.5).*
