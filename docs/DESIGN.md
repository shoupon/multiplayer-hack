# Family Coordination Layer: MVP design

Status: proposed, 2026-11-11. Revised the same day: Python instead of TypeScript, SQLite
instead of PostgreSQL, so the whole system runs and tests on one laptop.
Source brief: "Engineering Handoff: Family Coordination Layer (FCL)".
Section references like (H§9) point at that brief.

## 1. Goal and scope

FCL lets personal AI agents owned by different family members exchange structured
messages, negotiate a plan, ask their humans for approval, and keep durable shared task
state. No agent has to share an LLM provider with another, and no family member has to
give up their own assistant.

The MVP proves one scenario end to end (H§13). The husband's agent connects through MCP.
The wife's companion agent connects through REST and asks her for approval. Together they
agree on a dinner time after one counterproposal.

In scope:

- Identity for humans and agents, families, invitations, membership revocation.
- Agent registry, capabilities, transports, discovery.
- Delegation grants and a deterministic authorization policy.
- Universal message envelope, durable inbox with cursor polling.
- Coordination tasks: proposals, counterproposals, per-participant acceptance, expiry,
  cancellation, human approval.
- Database-backed job queue: expiry, follow-up reminders, webhook delivery.
- Signed webhook delivery with SSRF protection.
- MCP server adapter (stdio) over the REST API.
- Runnable two-agent demo, including a server restart mid-negotiation.

Single-laptop constraint: hosting, the demo, and the full test suite run on one machine
with Python 3.10+ and nothing else installed. No Docker, no database server, no cloud
service. State lives in one SQLite file.

Out of scope for the MVP:

- A2A gateway (designed in section 12, built in Phase 4).
- Calendar, restaurant, or any other external side effect (H§13).
- Messaging bridges (LINE, Telegram).
- Human login via OIDC or passkeys (section 7 explains the token-based stand-in).

## 2. Terms

| Term | Meaning |
|---|---|
| User (human principal) | A family member who owns data and authorizes actions. |
| Agent | One AI agent acting for exactly one user. A user may own many agents. |
| Transport | How an agent reaches FCL or FCL reaches the agent: `inbox` (poll), `webhook` (push), `mcp` (tool calls). |
| Family | A tenant. A user may belong to several. |
| Grant | A delegation from a user to one of their own agents: one action, in one family, optionally toward one target user. |
| Task | A durable coordination between participants, driven by a state machine. |
| Approval request | A parked agent action that waits for the agent's human to approve that exact action. |

One human is never assumed to be one agent, and one agent is never assumed to be one
provider. The `provider` field on an agent is a self-reported label. It never implies a
verified connection.

## 3. Stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.10+ | Owner's choice. 3.10 is the floor because it is what the target laptop ships. |
| HTTP | FastAPI on uvicorn | Pydantic validation at the boundary, generated OpenAPI docs at `/docs` for manual testing, `TestClient` for in-process integration tests. |
| Validation | Pydantic v2 | Parse every external input at the boundary into typed values. Strict models reject unknown keys. |
| Database | SQLite through the standard library `sqlite3`, WAL mode | One file, no server, survives restarts. Supports what the design needs: composite foreign keys, partial unique indexes, CHECK constraints, `RETURNING`. |
| DB access | Plain SQL migrations and small repository functions | The safety properties live in constraints. An ORM would add a second schema source of truth. |
| Jobs | `scheduled_jobs` table, drained by a worker thread inside the server process | No extra infrastructure (H§14). Doubles as the transactional outbox. |
| Outbound HTTP | `httpx` | Webhook delivery, the MCP adapter's REST calls, the demo agents. |
| MCP | `mcp` Python SDK, stdio server | Lets an MCP-capable assistant call FCL tools. |
| Tests | pytest | Integration tests drive the real app over HTTP against a temporary SQLite file. |

Architecture style: one modular monolith process (H§14) that serves HTTP and runs the job
worker, plus the MCP adapter as a separate thin process that talks REST.

SQLite allows one writer at a time. Every command opens its transaction with
`BEGIN IMMEDIATE`, which takes the write lock up front, so commands serialize and a
read-modify-write on a task cannot interleave with another. For a family-scale deployment
this throughput is ample. Moving to PostgreSQL later means replacing the connection
module and a handful of SQLite-specific statements; the repository functions keep their
signatures.

## 4. Repository layout

```text
pyproject.toml
docs/                      design, plan, architecture, protocol, security, integrations
fcl/
  __main__.py              CLI: `python -m fcl serve | migrate`
  config.py                settings from environment variables, injectable clock
  ids.py                   prefixed identifiers
  db.py                    connections, BEGIN IMMEDIATE transactions, migration runner
  migrations/              ordered plain-SQL migrations
  errors.py                AppError and constructors
  auth.py                  token resolution, principal types, FastAPI dependencies
  policy.py                pure authorization policy
  machine.py               pure task state machine
  envelope.py              message type registry and payload models
  identity.py              users and credentials
  families.py              families, memberships, invitations
  agents.py                agent registry, capabilities, transports, discovery
  grants.py                delegation grants, policy facts loader
  messaging.py             send, inbox, acknowledgement, read
  tasks.py                 coordination task service
  approvals.py             approval requests and decisions
  jobs.py                  queue, worker thread, expiry and reminder handlers
  webhooks.py              subscriptions, signing, SSRF guard, delivery handler
  audit.py                 audit writer and reader
  api.py                   FastAPI app factory and routes
  mcp_server.py            MCP stdio server: FCL tools mapped onto the REST API
demo/                      dinner scenario with two simulated agents
tests/                     unit tests for pure cores, integration tests over HTTP
```

Each domain module owns its SQL and service functions. `api.py` is the only module that
knows about HTTP. Pure cores (`policy.py`, `machine.py`, `envelope.py`) import nothing
from HTTP or the database.

## 5. Components and trust boundaries

```text
 MCP assistant ──stdio──> FCL MCP server ──HTTPS + agent token──┐
 Custom agent ───────────────HTTPS + agent token───────────────┤
 Companion UI / human ───────HTTPS + user token────────────────┤
                                                               v
                         ┌──────────── FCL API (FastAPI) ────────────┐
   Boundary 1 ─────────> │ authenticate -> parse (Pydantic) -> authz │
                         │ services (families, agents, messaging,    │
                         │ coordination, approvals, audit)           │
                         └──────────────┬────────────────────────────┘
                                        │ one transaction per command
                                        v
                         ┌──────────── SQLite file (WAL) ────────────┐
                         │ domain tables + scheduled_jobs (outbox)   │
                         └──────────────┬────────────────────────────┘
                                        │ claim with lease
                                        v
                         ┌──────── Job worker thread ────────────────┐
   Boundary 2 <───────── │ expiry, reminders, webhook delivery       │
                         └───────────────────────────────────────────┘
```

| Boundary | Trust rule |
|---|---|
| 1. Client to API | Everything is untrusted: headers, body, the claimed sender, and any instruction inside message text. Identity comes only from the bearer token. Bodies are parsed by strict Pydantic models. |
| 2. Worker to webhook receivers | Outbound requests to user-supplied URLs. Destinations are validated and the resolved IP is pinned (section 13). Payloads are signed. |
| 3. MCP server process | Holds exactly one agent token. It is a REST client with no database access, so it cannot exceed that agent's grants. |
| 4. Database | Trusted. Composite foreign keys still make cross-family references unrepresentable. `PRAGMA foreign_keys = ON` is set on every connection, because SQLite leaves enforcement off by default. |

## 6. Data model

All identifiers are opaque strings with a type prefix (`usr_`, `fam_`, `agt_`, `crd_`,
`grt_`, `inv_`, `cnv_`, `msg_`, `tsk_`, `apr_`, `whk_`). SQLite storage conventions:
timestamps are UTC ISO-8601 text in one fixed format (`2026-11-11T10:00:00.000000Z`),
so string comparison is time comparison. JSON columns are text. Booleans are 0 or 1.
Sequence columns (`seq`, `id` on log tables) are `INTEGER PRIMARY KEY AUTOINCREMENT`,
which never reuses a value, so an inbox cursor never skips or repeats.

| Table | Purpose and key constraints |
|---|---|
| `users` | Human principals. |
| `credentials` | Bearer tokens for users or agents. Stores `sha256(token)` only, unique. Exactly one of `user_id` or `agent_id`, matching `principal_kind`. `revoked_at`, `expires_at`, `last_used_at`. |
| `families` | Tenants. |
| `family_memberships` | PK `(family_id, user_id)`. `role` is `admin` or `member`. `status` is `active` or `removed`. |
| `family_invitations` | One-time code stored as a hash, `expires_at`, `accepted_by`, `revoked_at`. |
| `agents` | `owner_user_id`, `display_name`, self-reported `provider`, `status`, `last_seen_at`. Unique `(id, owner_user_id)` so grants can reference the pair. |
| `agent_capabilities` | PK `(agent_id, capability)`. Declared capabilities such as `receive_message`, `propose_schedule`, `request_approval`. |
| `agent_transports` | PK `(agent_id, kind)`. Declared transports and observed delivery health: `last_success_at`, `last_failure_at`. |
| `delegation_grants` | `agent_id`, `grantor_user_id`, `family_id`, `action`, nullable `target_user_id`, `requires_approval`, `expires_at`, `revoked_at`. FK `(agent_id, grantor_user_id)` to `agents(id, owner_user_id)`, so only an agent's owner can delegate to it. FKs from `(family_id, grantor_user_id)` and `(family_id, target_user_id)` to memberships. |
| `conversations` | `family_id`, subject. Groups messages. |
| `messages` | The envelope (section 9). Unique `(sender_agent_id, idempotency_key)`. FK `(conversation_id, family_id)` to `conversations(id, family_id)`. |
| `message_deliveries` | One row per recipient agent. `seq` is a global autoincrement and the inbox cursor. `status` is `pending`, `delivered`, `acknowledged`, or `failed`. Unique `(message_id, recipient_agent_id)`. |
| `coordination_tasks` | `state`, `proposal` (JSON), `proposal_version`, `negotiation_count`, `max_negotiations`, `expires_at`, `outcome`, `row_version`. Unique `(initiator_agent_id, idempotency_key)`. |
| `task_participants` | PK `(task_id, user_id)`. `agent_id`, `role`, `response` (`pending`, `accepted`, `declined`), `responded_version`. FK `(task_id, family_id)` to the task and `(family_id, user_id)` to memberships. |
| `task_transitions` | Append-only task event log: `from_state`, `to_state`, `event` (JSON), actor, linked `message_id`. Unique `(task_id, actor_agent_id, idempotency_key)`. |
| `approval_requests` | `approver_user_id`, `requesting_agent_id`, `action` (the exact parked command, JSON), `action_hash`, `status` (`pending`, `granted`, `denied`, `expired`, `superseded`), `expires_at`, decision fields. Partial unique index: one pending request per `(task_id, requesting_agent_id)`. |
| `audit_events` | Append-only: actor (kind, user, agent, credential), action, resource, outcome (`allowed`, `denied`, `succeeded`, `failed`), reason, redacted metadata. Denials are recorded too. |
| `webhook_subscriptions` | `agent_id`, `url`, `secret_ciphertext` (AES-256-GCM), `status`. |
| `scheduled_jobs` | `kind`, `payload`, `run_at`, `status` (`pending`, `running`, `done`, `dead`), `attempts`, `max_attempts`, `locked_until`, `last_error`, unique nullable `dedupe_key`. |

### Tenant isolation

Every family-scoped row carries `family_id`. Child rows reference parents through
composite keys that include `family_id`, for example `message_deliveries(message_id,
family_id) -> messages(id, family_id)`. A row that points across families cannot be
inserted. Every repository read takes a `FamilyId` taken from an authorized context,
never from the request body alone. SQLite has no row-level security; a later PostgreSQL
port can add it (section 15).

### Revocation and deletion

Removing a member sets `family_memberships.status = removed`. Grants that name the
removed user as grantor or target stop matching, because the policy checks membership
status on every call. Revoking a credential or grant sets `revoked_at` and takes effect on
the next request. Retention rules for message content and audit records are listed in
section 15 and are configuration, not code, in the MVP.

## 7. Identity and authentication

- Two token kinds: user tokens (`fcl_u_…`) and agent tokens (`fcl_a_…`), each 32 random
  bytes in base64url. FCL stores `sha256(token)`. A high-entropy random token does not need
  a slow password hash. Lookup is one indexed query, so there is no timing oracle on the
  secret.
- A token is shown once, at creation.
- Agent tokens are issued by the agent's owner (`POST /v1/agents`,
  `POST /v1/agents/{id}/credentials`). Several can be active at once, which allows
  rotation without downtime. Each can be revoked on its own.
- The authenticated principal is a discriminated union, `{ kind: 'user', userId }` or
  `{ kind: 'agent', agentId, ownerUserId }`, plus the credential id. Routes declare which
  kind they accept.
- The sender of every message and the actor of every transition come from this principal.
  Sender fields in request bodies are rejected by the schema.

**MVP bootstrap, flagged.** `POST /v1/users` creates a user and returns a user token with
no prior authentication. That is acceptable for a local demo and unacceptable for a public
deployment. Before any public exposure, user tokens must come from a real login (OIDC or
passkeys) and signup must be invitation-gated. The token format and the principal model do
not change when that happens.

## 8. Authorization

Authorization is a pure function over data loaded from the database. No LLM is consulted
and no message text is read.

### Actions

| Action | Who holds it | Meaning |
|---|---|---|
| `agent:message:send` | Sending agent | Send direct messages to a target user's agents in a family. |
| `agent:message:receive` | Receiving agent | Recipient-side consent: accept messages and tasks from a family or one sender. Also makes the agent discoverable to that audience. |
| `family:members:discover` | Agent | List reachable agents in a family. |
| `task:propose` | Agent | Create a coordination task with a target participant. Also allows cancelling tasks it created. |
| `task:respond` | Agent | Accept, decline, counterpropose, or complete a task it participates in. May carry `requires_approval`. |
| `calendar:availability:read`, `calendar:event:create`, `task:execute` | Reserved | No connector exists in the MVP. Grant creation rejects them. |

Approval decisions (`task:approve` in the brief) are not delegable in the MVP. Only the
named human approver, authenticated with a user token, can decide. An agent token on
`POST /v1/approvals/{id}/decisions` gets 403 even when it belongs to the approver.

### Evaluation

For an agent acting in family F toward target user T:

1. The credential is valid, unexpired, and unrevoked. The agent is active.
2. The agent's owner is an active member of F.
3. T, if present, is an active member of F.
4. A grant exists with matching agent, family, and action, a `target_user_id` that is null
   or T, unexpired and unrevoked.
5. For delivery to another agent, the recipient agent holds a matching
   `agent:message:receive` grant whose target is null or the sender's user.

If several grants match, the most specific one wins (target-specific beats family-wide).
On a tie, the one with `requires_approval = true` wins. The decision is
`allow(grant, requiresApproval)` or `deny(reason)`. The authorizing grant's id is stored
on the resulting message or transition as the delegation reference.

`requires_approval` is accepted only on `task:respond` in the MVP. Parking a
`task:propose` behind approval is a Phase 2+ extension.

Users act as themselves without grants, inside families where they are active members:
creating invitations (admin only), registering their own agents, granting and revoking,
reading tasks they participate in, deciding their own approvals.

### Discovery

`GET /v1/agents?family_id=F` returns agents of other members of F that hold an
`agent:message:receive` grant covering the caller's user. A user always sees their own
agents. The response contains id, owner user id, display name, declared capabilities,
transport kinds, and derived availability. It never contains webhook URLs, credentials,
grants, or other families.

### Approval scope

An approval request stores the exact parked command (task id, response kind,
`proposal_version`, proposal body) and its hash. Granting it applies that command once.
If the task moved on in the meantime (a newer `proposal_version`, a terminal state), the
command is stale and the approval becomes `superseded` instead of applying anything.
Approval never widens a grant.

## 9. Message envelope

```json
{
  "message_id": "msg_…",
  "schema_version": "1.0",
  "family_id": "fam_…",
  "conversation_id": "cnv_…",
  "task_id": "tsk_…",
  "type": "coordination.proposal",
  "payload_schema": "fcl.coordination.proposal/1",
  "payload": { "intent": "schedule_dinner", "proposed_time": "2026-11-13T19:00:00+08:00", "duration_minutes": 90 },
  "sender": { "kind": "agent", "user_id": "usr_…", "agent_id": "agt_…" },
  "auth": { "credential_id": "crd_…", "grant_id": "grt_…" },
  "recipients": [{ "user_id": "usr_…", "agent_id": "agt_…", "delivery_status": "pending" }],
  "correlation_id": "…",
  "causation_id": "msg_…",
  "idempotency_key": "…",
  "requires_response": true,
  "expires_at": "2026-11-13T12:00:00+08:00",
  "created_at": "…"
}
```

| Field | Source |
|---|---|
| `message_id`, `created_at`, `schema_version`, `payload_schema`, `sender`, `auth`, delivery status | Server |
| `family_id`, recipient agent, `type`, `payload`, `conversation_id`, `correlation_id`, `causation_id`, `requires_response`, `expires_at` | Client, validated |
| `idempotency_key` | `Idempotency-Key` request header, required on every create |
| `task_id` | Server, for engine-generated messages |

A registry maps each `type` to a payload schema and a route of origin:

| Type | Origin |
|---|---|
| `message.text`, `information.request`, `information.response` | `POST /v1/messages` |
| `coordination.proposal`, `.counterproposal`, `.accept`, `.decline`, `.cancel` | Task engine, produced by task endpoints |
| `approval.request`, `approval.granted`, `approval.denied` | Approval engine |
| `task.status`, `task.completed`, `task.failed` | Task engine and reminder job |

`POST /v1/messages` rejects engine-only types with 422 and names the task endpoint to use.
That keeps one writer per state transition.

Delivery status is separate from business acceptance. `delivered` means the recipient
agent fetched the message. It never moves a task to `AGREED` or `COMPLETED`.

Incoming payload text is untrusted data. FCL never interprets it. Text fields have length
limits. The MCP adapter labels message content as untrusted when it returns it to the
calling assistant.

Versioning: `schema_version` is the envelope version. `payload_schema` is
`fcl.<type>/<major>`. Additive optional fields keep the major version. A breaking change
adds a new major and FCL accepts both during migration.

## 10. Coordination state machine

The machine is a pure reducer: `apply(task, event, now) -> { task, transitions } | error`.
Inside one `BEGIN IMMEDIATE` transaction, the service loads the task, applies the event,
and persists the new row, the transition rows, the generated messages, and any jobs in one
transaction.

States: `CREATED`, `AWAITING_DELIVERY`, `AWAITING_RESPONSE`, `COUNTERPROPOSED`,
`AWAITING_APPROVAL`, `AGREED`, `COMPLETED`, `DECLINED`, `EXPIRED`, `CANCELLED`, `FAILED`.
Terminal: `COMPLETED`, `DECLINED`, `EXPIRED`, `CANCELLED`, `FAILED`.

| From | Event | To | Rule |
|---|---|---|---|
| (new) | `create` | `CREATED` then `AWAITING_DELIVERY` | Initiator counts as accepted at version 1. |
| `AWAITING_DELIVERY` | `delivered` | `AWAITING_RESPONSE` | Every invitee's agent has fetched the proposal. |
| open | `accept` | same open state, or `AGREED` | `AGREED` when every participant accepted the current version. |
| open | `counterpropose` | `COUNTERPROPOSED` | Version + 1, negotiation count + 1, all responses reset to pending except the author. Rejected past `max_negotiations`. |
| open | `decline` | `DECLINED` | One decline ends the task in the MVP. |
| open | `request_approval` | `AWAITING_APPROVAL` | Response parked behind an approval. |
| `AWAITING_APPROVAL` | approved response | as for that response | Applied with the original `proposal_version`. |
| `AWAITING_APPROVAL` | `approval_denied` | open state | Parked response dropped. |
| non-terminal | `cancel` | `CANCELLED` | Initiator only. |
| non-terminal | `expire` | `EXPIRED` | `now >= expires_at`. |
| `AGREED` | `complete` | `COMPLETED` | Explicit participant action. Never inferred from delivery. |
| non-terminal | `fail` | `FAILED` | System only, for unrecoverable delivery or invariant failure. |

"Open" means `AWAITING_DELIVERY`, `AWAITING_RESPONSE`, or `COUNTERPROPOSED`. A response
that arrives while the task is still `AWAITING_DELIVERY` is valid; the recipient clearly
received the proposal. After an approval resolves, the open state is derived from the
data: `COUNTERPROPOSED` if `proposal_version > 1`, else `AWAITING_RESPONSE`.

Guards checked by the reducer, in order:

1. Terminal state: `409 task_terminal`.
2. Expired (`now >= expires_at`): `409 task_expired`. The service also records the
   `EXPIRED` transition in the same call.
3. Actor is not a participant: `403`.
4. `proposal_version` differs from the current one: `409 stale_version`. This rejects late
   and out-of-order responses.
5. Same participant already gave the same response to the same version: success with
   `result: noop`. A duplicate is harmless.
6. Participant has a pending approval: `409 approval_pending`.
7. Negotiation cap reached: `422 negotiation_limit`.

## 11. Idempotency, ordering, and consistency

- Every create endpoint requires an `Idempotency-Key` header. Uniqueness is enforced by
  the domain table itself: `messages(sender_agent_id, idempotency_key)`,
  `coordination_tasks(initiator_agent_id, idempotency_key)`,
  `task_transitions(task_id, actor_agent_id, idempotency_key)`. A retry finds the existing
  row and returns it. A retry with the same key and a different body gets
  `422 idempotency_key_reused`. Because the key check and the write share one
  transaction, a crash between them cannot create a duplicate.
- Each command runs in one transaction that writes domain rows, messages, deliveries, and
  `scheduled_jobs` together. That table is the transactional outbox. Nothing is sent to
  the network inside the transaction.
- Concurrent commands serialize on SQLite's write lock, taken up front by
  `BEGIN IMMEDIATE`, so two responses to one task cannot interleave.
- The worker claims a due job inside a write transaction by setting `status = running` and
  `locked_until`. A crashed worker's job becomes claimable again after the lease. Every handler is idempotent: it
  reads current state and applies only the remaining difference.
- Jobs with a natural identity use `dedupe_key` (`expire:<task>`,
  `reminder:<task>:<version>`, `webhook:<delivery_seq>`), so re-enqueueing is a no-op.

## 12. API contracts

All routes are under `/v1`, take and return JSON, and authenticate with
`Authorization: Bearer <token>`. Errors are `{ "error": { "code": "…", "message": "…" } }`.

| Method and path | Principal | Purpose |
|---|---|---|
| `POST /users` | none (MVP bootstrap) | Create a user, return a user token once. |
| `GET /me` | user or agent | Who am I. |
| `POST /families` | user | Create a family. Caller becomes admin. |
| `GET /families/{id}` | member | Family and member list. |
| `POST /families/{id}/invitations` | admin | Create a one-time invitation code. |
| `POST /invitations/accept` | user | Join with a code. |
| `DELETE /families/{id}/members/{userId}` | admin, or self | Remove membership. |
| `POST /agents` | user | Register an own agent with capabilities and transports. Returns the agent token once. |
| `GET /agents?family_id=` | user or agent | Discovery (section 8). |
| `GET /agents/{id}/capabilities?family_id=` | user or agent | Same visibility as discovery, within one family. |
| `POST /agents/{id}/credentials` | owner | Issue an extra agent token (rotation). |
| `DELETE /agents/{id}/credentials/{credentialId}` | owner | Revoke one token. |
| `POST /grants` | owner | Delegate an action to an own agent. |
| `GET /grants?agent_id=` | owner | List grants. |
| `DELETE /grants/{id}` | owner | Revoke. |
| `POST /messages` | agent | Send a direct message. Requires `Idempotency-Key`. |
| `GET /messages/{id}` | sender or recipient agent, or their owners | Read one envelope. |
| `GET /agents/{id}/inbox?after=<cursor>&limit=` | that agent | Poll deliveries after a cursor. Marks them `delivered`. |
| `POST /agents/{id}/inbox/ack` | that agent | `{ "through": <cursor> }` marks deliveries `acknowledged`. |
| `POST /tasks` | agent | Create a coordination task. Requires `Idempotency-Key`. |
| `GET /tasks?state=` | participant user or agent | List. |
| `GET /tasks/{id}` | participant user or agent | Task, participants, full transition history. |
| `POST /tasks/{id}/responses` | participant agent | `accept`, `decline`, `counterpropose`, `complete`. Requires `Idempotency-Key`. |
| `POST /tasks/{id}/cancel` | initiator agent | Cancel. |
| `GET /approvals?status=` | user | Own approval requests. |
| `POST /approvals/{id}/decisions` | approver user only | `{ "decision": "grant" \| "deny" }`. |
| `POST /webhooks` | owner | Subscribe an own agent. Returns the signing secret once. |
| `DELETE /webhooks/{id}` | owner | Unsubscribe. |
| `GET /audit` | user | Events performed by the user or the user's agents. |

Create a task:

```http
POST /v1/tasks
Authorization: Bearer fcl_a_…
Idempotency-Key: 6f1c…

{
  "family_id": "fam_…",
  "invitees": [{ "agent_id": "agt_wife" }],
  "intent": "schedule_dinner",
  "proposal": { "proposed_time": "2026-11-13T19:00:00+08:00", "duration_minutes": 90 },
  "expires_at": "2026-11-13T12:00:00+08:00"
}
```

Respond:

```http
POST /v1/tasks/tsk_…/responses
Authorization: Bearer fcl_a_…
Idempotency-Key: 9a2e…

{ "kind": "counterpropose", "proposal_version": 1,
  "proposal": { "proposed_time": "2026-11-13T19:30:00+08:00", "duration_minutes": 90 } }
```

The response says `"result": "applied"`, `"noop"`, or `"pending_approval"` (with
`approval_id`), and includes the task.

Poll the inbox:

```http
GET /v1/agents/agt_…/inbox?after=0&limit=50
→ { "items": [ { "cursor": 12, "message": { …envelope… } } ], "next_cursor": 12 }
```

## 13. Webhook delivery

- Signature header `FCL-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>`.
  Receivers reject timestamps older than 5 minutes and deduplicate on `message_id`.
- Delivery job per delivery row. Exponential backoff with jitter, `max_attempts` 8, then
  status `dead` (dead letter). The message stays in the inbox, so a dead webhook never
  loses a message.
- SSRF protection: `https` only, except plain `http` to loopback when
  `FCL_ALLOW_LOOPBACK_WEBHOOKS=true` (single-laptop development and the demo); no credentials in the URL;
  hostname resolved once per attempt; private, loopback, link-local, CGNAT, multicast,
  and metadata ranges rejected for every resolved address; the connection is pinned to
  the vetted IP so DNS rebinding cannot swap it; redirects are not followed; 5 second
  timeout; response body discarded.
- Observability: `agent_transports.last_success_at` and `last_failure_at`, job
  `attempts` and `last_error`.

## 14. MCP adapter and A2A gateway

**MCP.** `fcl/mcp_server.py` runs over stdio with `FCL_BASE_URL` and `FCL_AGENT_TOKEN`.
Tools: `fcl_list_family_agents`, `fcl_get_agent_capabilities`, `fcl_send_message`,
`fcl_get_inbox`, `fcl_create_coordination_task`, `fcl_get_task_status`,
`fcl_respond_to_proposal`, `fcl_request_approval` (explains that approvals are raised by
FCL when a grant requires one), `fcl_get_pending_approvals` (the agent's own parked
requests). Every tool is a REST call made with the agent token. The server enforces
authorization; the MCP process enforces nothing. MCP gives the assistant tools to call
when it runs. It gives FCL no way to wake the assistant, so inbound messages wait in the
durable inbox until the assistant calls `fcl_get_inbox`.

**A2A (Phase 4).** A gateway module translates between A2A and FCL objects. An external
agent's Agent Card is fetched over HTTPS from an allowlisted origin and pinned per family.
A2A `message/send` becomes an FCL message, and an A2A task maps to an FCL coordination task
only when the semantics match (proposal and response). A2A-specific fields live in an
`extensions.a2a` object, never in core columns. The current A2A specification and SDK
must be checked before this is built.

## 15. Security decisions that need the owner's call

1. Unauthenticated `POST /v1/users` bootstrap (section 7). Fine locally, must be replaced
   before public exposure.
2. Approvals are human-only and non-delegable in the MVP (section 8).
3. One decline ends a task, including future multi-party tasks (section 10).
4. Tenant isolation is query-boundary plus composite FKs. SQLite offers no row-level
   security.
5. Retention defaults: message payloads 180 days, audit events 400 days, purged by a job.
   Account removal deletes the user's credentials and agents and redacts their message
   payloads, while keeping audit rows with ids only.
6. Webhook signing secrets are encrypted with one key (`FCL_ENCRYPTION_KEY`). When it is
   unset, the server generates one into a `0600` key file next to the database. An OS
   keychain or KMS is the production target.
7. The server binds to `127.0.0.1` by default. Binding to another interface is an
   explicit setting, because item 1 makes an exposed server open to signups.

## 16. Key technical risks

| Risk | Mitigation |
|---|---|
| Consumer assistants (ChatGPT, Grok, Claude apps) expose no addressable inbox. | Durable inbox plus companion agent. No claims of direct integration. Provider adapters are evaluated one at a time against official APIs. |
| MCP cannot wake an assistant, so coordination stalls until the human opens it. | Reminder job and, later, human notification bridges (LINE, Telegram). |
| Prompt injection through message text. | Text never feeds authorization. Strict payload schemas, length limits, untrusted-content labelling in the MCP adapter. |
| Multi-party semantics (`AWAITING_APPROVAL` is task-level). | Per-participant state is already stored. The task-level state is a projection that can be refined without a data migration. |
| SQLite allows one writer at a time. | Short transactions, WAL mode, `busy_timeout`. Ample for one family on one laptop. Repository functions isolate SQL for a later PostgreSQL port. |
| SQLite leaves foreign keys off by default. | The connection factory sets `PRAGMA foreign_keys = ON`; a test asserts a cross-family insert fails. |
| Single-key secret encryption and token bootstrap. | Flagged in section 15, owner decision before public launch. |

## 17. Implementation sequence

See `docs/IMPLEMENTATION_PLAN.md`.
