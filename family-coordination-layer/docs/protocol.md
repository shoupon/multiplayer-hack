# FCL protocol reference

This page describes the FCL HTTP API as implemented in `fcl/api.py`, version `0.1.0`. A running
server also serves a generated OpenAPI reference at `/docs` and `/openapi.json`.

## Conventions

- Base path: `/v1`. Requests and responses are JSON with `content-type: application/json`.
- Times are ISO 8601 with an offset. Fields typed as a datetime reject a time without one.
- Request bodies reject unknown fields.
- A `DELETE` succeeds with `204` and no body.
- The server binds to `127.0.0.1:8000` by default.

## Authentication

Every endpoint except `POST /v1/users` needs `Authorization: Bearer <token>`. Tokens are opaque.
Their prefix tells you the kind of principal:

| Prefix | Principal | Issued by |
|---|---|---|
| `fcl_u_` | A person | `POST /v1/users` |
| `fcl_a_` | An agent, acting for its owner | `POST /v1/agents`, `POST /v1/agents/{agent_id}/credentials` |

The server returns a token once and stores only its SHA-256 hash. A missing, unknown, revoked,
or expired token gets `401 unauthenticated`. A token of the wrong kind gets `403` with
`user_credential_required` or `agent_credential_required`.

## Identifiers

An identifier is a prefix, an underscore, and 24 hex characters, such as
`tsk_038c31079a660639f8fcca30`. A path or field that holds the wrong kind of id fails with
`400 invalid_request`.

| Prefix | Kind | Prefix | Kind |
|---|---|---|---|
| `usr` | user | `cnv` | conversation |
| `fam` | family | `msg` | message |
| `inv` | invitation | `tsk` | coordination task |
| `agt` | agent | `apr` | approval request |
| `crd` | credential | `whk` | webhook subscription |
| `grt` | grant | | |

## Errors

Every error has the same body:

```json
{"error": {"code": "stale_version", "message": "response is for version 1, current is 2"}}
```

| Status | Codes |
|---|---|
| 400 | `invalid_request`, `invalid_payload`, `unknown_message_type`, `idempotency_key_required`, `invalid_expiry`, `unknown_scope`, `invalid_params`, `invalid_webhook_url`, `webhook_destination_blocked` |
| 401 | `unauthenticated` |
| 403 | `user_credential_required`, `agent_credential_required`, `agent_disabled`, `admin_required`, `no_grant`, `actor_not_member`, `recipient_not_accepting`, `not_your_inbox`, `not_participant`, `not_initiator`, `not_approver` |
| 404 | `not_found` |
| 409 | `task_terminal`, `task_expired`, `stale_version`, `approval_pending`, `invalid_transition`, `approval_expired`, `approval_not_pending`, `approval_superseded`, `last_admin`, `webhook_exists` |
| 413 | `body_too_large` |
| 422 | `idempotency_key_reused`, `engine_only_type`, `negotiation_limit`, `target_not_member`, `duplicate_invitee`, `duplicate_participant`, `approval_not_supported`, `invalid_expiry` |
| 429 | `rate_limited` |

A resource in a family you do not belong to returns `404`, not `403`, so ids from other
families reveal nothing. The server records denied authorization checks in the audit log.

## Idempotency

These requests need an `Idempotency-Key` header of 1 to 200 characters from
`[A-Za-z0-9_-:.]`:

- `POST /v1/messages`
- `POST /v1/tasks`
- `POST /v1/tasks/{task_id}/responses`
- `POST /v1/approvals`

Keys are scoped to the sending agent. The first request with a key returns `201` when it
creates something. A repeat with the same key and the same body returns the original result
with `200` and changes nothing. A repeat with the same key and a different body fails with
`422 idempotency_key_reused`.

## Limits

| Limit | Value |
|---|---|
| Request body | 64 KiB, else `413 body_too_large` |
| Requests per credential per minute | 600 (`FCL_RATE_LIMIT_PER_MINUTE`), else `429 rate_limited`. Unauthenticated requests count per client address. |
| Invitees per task | 10 |
| Counterproposals per task | 5 (`FCL_MAX_NEGOTIATIONS`), else `422 negotiation_limit` |
| Task lifetime | `expires_at` must be in the future and at most 336 hours away (`FCL_MAX_TASK_TTL_HOURS`) |
| Permission request lifetime | at most 24 hours |
| Inbox page | `limit` up to 200, default 50 |

## Endpoints

The **Credential** column says which token kind the endpoint accepts.

### Identity and families

| Method and path | Credential | Does |
|---|---|---|
| `POST /v1/users` | none | Creates a user from `{display_name}`. Returns `{id, display_name, token}`. |
| `GET /v1/me` | any | Describes the caller. For an agent: `{kind, agent_id, owner_user_id}`. |
| `POST /v1/families` | user | Creates a family from `{name}`. The caller becomes its admin. |
| `GET /v1/families/{family_id}` | any | Returns the family and its active members. |
| `POST /v1/families/{family_id}/invitations` | user, admin | Creates a single-use invitation from `{role?, ttl_hours?}`. `role` is `member` (default) or `admin`. `ttl_hours` is 1 to 720, default 72. Returns a `code` once. |
| `POST /v1/invitations/accept` | user | Joins the family named by `{code}`. |
| `DELETE /v1/families/{family_id}/members/{user_id}` | user, admin | Removes a member. The last admin cannot be removed (`409 last_admin`). |

### Agents and grants

| Method and path | Credential | Does |
|---|---|---|
| `POST /v1/agents` | user | Registers an agent owned by the caller. Body: `{display_name, provider, capabilities?, transports?, grants?}`. Returns the agent plus `credential_id` and `token`. |
| `GET /v1/agents?family_id=` | any | Lists the agents in the family the caller may see. An agent caller needs `family:members:discover`. |
| `GET /v1/agents/{agent_id}/capabilities?family_id=` | any | Returns one visible agent's capabilities and transports. |
| `POST /v1/agents/{agent_id}/credentials` | user, owner | Issues another token for the agent from `{label?}`. |
| `DELETE /v1/agents/{agent_id}/credentials/{credential_id}` | user, owner | Revokes one token. |
| `DELETE /v1/agents/{agent_id}` | user, owner | Disables the agent. Its tokens then fail with `403 agent_disabled`. |
| `POST /v1/grants` | user, owner | Grants one action to an agent: `{agent_id, family_id, action, target_user_id?, requires_approval?, expires_at?}`. |
| `GET /v1/grants?agent_id=` | user, owner | Lists the agent's grants. |
| `DELETE /v1/grants/{grant_id}` | user, owner | Revokes a grant. |

`provider` is a free-text label such as `custom-mcp`. FCL never verifies it, and discovery
reports `provider_verified: false`. `transports` lists `inbox`, `mcp`, or both. A capability is a
lowercase identifier such as `propose_schedule`.

Discovery returns the caller's own agents, plus other members' agents whose owners granted
`agent:message:receive` to the caller or to the whole family. It never returns credentials,
grants, or webhook URLs.

### Messages and inbox

| Method and path | Credential | Does |
|---|---|---|
| `POST /v1/messages` | agent | Sends a direct message. Body: `{family_id, to_agent_id, type, payload, conversation_id?, correlation_id?, causation_id?, requires_response?, expires_at?}`. Returns the envelope. |
| `GET /v1/messages/{message_id}` | any | Returns one message the caller sent or received. |
| `GET /v1/agents/{agent_id}/inbox?after=&limit=` | agent, self | Returns deliveries after cursor `after`, oldest first, as `{items: [{cursor, message}], next_cursor}`. |
| `POST /v1/agents/{agent_id}/inbox/ack` | agent, self | Marks every delivery up to `{through}` as acknowledged. Returns `{acknowledged}`, the count. |

Sending needs `agent:message:send` toward the recipient's owner. The recipient's agent must also
hold `agent:message:receive` toward the sender's owner, else `403 recipient_not_accepting`. Only
`direct` message types can be sent this way. Engine types fail with `422 engine_only_type`.

### Coordination tasks

| Method and path | Credential | Does |
|---|---|---|
| `POST /v1/tasks` | agent | Creates a task: `{family_id, invitees, intent, proposal, expires_at}`. `invitees` is a list of agent ids. Returns the task. |
| `GET /v1/tasks?state=&limit=` | any | Lists tasks the caller takes part in, newest change first. |
| `GET /v1/tasks/{task_id}` | any | Returns one task with its `history`. |
| `POST /v1/tasks/{task_id}/responses` | agent | Answers the current proposal: `{kind, proposal_version, proposal?, reason?, evidence?}`. |
| `POST /v1/tasks/{task_id}/cancel` | agent, initiator | Cancels the task from `{reason?}`. |

`intent` is a lowercase identifier such as `schedule_dinner`. A `proposal` has at least one of
`proposed_time`, `duration_minutes`, `location`, and `note`.

### Approvals

| Method and path | Credential | Does |
|---|---|---|
| `POST /v1/approvals` | agent | Asks the agent's owner for a permission: `{family_id, scope, params, purpose, expires_at, task_id?}`. Returns the request. |
| `GET /v1/approvals?status=` | user | Lists requests the caller must decide. `status` is `pending`, `granted`, `denied`, `expired`, or `superseded`. |
| `GET /v1/approvals/{approval_id}` | any | Returns one request. Only the approver and the requesting agent can read it. |
| `POST /v1/approvals/{approval_id}/decisions` | user, approver | Decides with `{decision}`, which is `grant` or `deny`. |

### Webhooks and audit

| Method and path | Credential | Does |
|---|---|---|
| `POST /v1/webhooks` | user, owner | Subscribes `{agent_id, url}`. Returns the signing `secret` once. One active subscription per agent (`409 webhook_exists`). |
| `DELETE /v1/webhooks/{webhook_id}` | user, owner | Unsubscribes. |
| `GET /v1/audit?after=&limit=` | user | Lists audit events about the caller and their agents, as `{items, next_after}`. |

## Grants

A grant lets one agent take one action in one family on its owner's behalf.

| Action | Lets the agent |
|---|---|
| `agent:message:send` | send direct messages to the target's agents |
| `agent:message:receive` | receive messages and proposals from the target's agents |
| `family:members:discover` | list the family's agents |
| `task:propose` | create a task that invites the target's agents |
| `task:respond` | answer a task whose current proposal the target wrote |

The rules for grants:

- `target_user_id` narrows a grant to one family member. Without it, the grant covers every
  member.
- `requires_approval` is allowed only on `task:respond`. Elsewhere it fails with
  `422 approval_not_supported`.
- When several grants match, the one with a target wins. Between equals, the one that requires
  approval wins.
- A revoked or expired grant never matches.
- If the agent's owner or the target is no longer an active member, the request is denied
  whatever the grants say.

## Message envelope

Every message, in an inbox page or a webhook body, has this shape:

| Field | Meaning |
|---|---|
| `message_id`, `family_id`, `conversation_id`, `task_id` | Where the message belongs. `task_id` is null for direct messages. |
| `schema_version` | `"1.0"` |
| `type`, `payload_schema`, `payload` | The message type, its schema id such as `fcl.coordination.proposal/1`, and the payload. |
| `content_trust` | `untrusted` if an agent wrote the payload, `system` if FCL did. |
| `sender` | `{kind, user_id, agent_id}`. `kind` is `agent` or `system`. |
| `recipients` | `[{user_id, agent_id, delivery_status}]`. Status is `pending`, `delivered`, `acknowledged`, or `failed`. |
| `correlation_id`, `causation_id` | Optional threading fields. Task messages use the task id as `correlation_id`. |
| `requires_response`, `expires_at`, `created_at` | As sent. |
| `idempotency_key`, `auth` | Returned only to the sender. `auth` is `{credential_id, grant_id}`. |

## Message types

Agents send `direct` types with `POST /v1/messages`. Only FCL sends `engine` types, so each
change of task or approval state has one writer.

| Type | Origin | Payload |
|---|---|---|
| `message.text` | direct | `{text}`, up to 4,000 characters |
| `information.request` | direct | `{topic, question, data?}` |
| `information.response` | direct | `{topic, answer, data?}`. `answer` is for people. `data` is the same answer as a JSON object another agent can parse. |
| `coordination.proposal` | engine | `{intent, proposal_version, proposal, expires_at}` |
| `coordination.counterproposal` | engine | Same as `coordination.proposal` |
| `coordination.accept` | engine | `{proposal_version}` |
| `coordination.decline` | engine | `{proposal_version, reason?}` |
| `coordination.cancel` | engine | `{reason?}` |
| `approval.request` | engine | `{approval_id, summary}` |
| `approval.granted`, `approval.denied` | engine | `{approval_id, kind?}` |
| `task.status` | engine | `{state, proposal_version, note?}` |
| `task.completed` | engine | `{outcome}` |
| `task.failed` | engine | `{reason}` |

## Inbox delivery

Each delivery has a cursor, a number that only increases for that agent. An agent reads with
`after` set to the last cursor it handled. Reading a delivery moves it from `pending` to
`delivered`. When a proposal is first delivered this way, the task moves from
`AWAITING_DELIVERY` to `AWAITING_RESPONSE`. An agent that has handled a page acknowledges it
with `POST .../inbox/ack {through: next_cursor}`. The inbox keeps messages after they are
acknowledged, so a crashed agent can read them again.

## Task lifecycle

A task holds one current proposal with a version number, starting at 1. The initiator's
proposal counts as the initiator's acceptance of version 1.

| State | Meaning |
|---|---|
| `CREATED` | Written. Moves to `AWAITING_DELIVERY` in the same transaction. |
| `AWAITING_DELIVERY` | At least one invitee's agent has not yet received the current proposal. |
| `AWAITING_RESPONSE` | Every participant's agent has the proposal, and someone has not answered. |
| `COUNTERPROPOSED` | The current proposal is version 2 or later. |
| `AWAITING_APPROVAL` | A participant's answer waits for that participant's approval. |
| `AGREED` | Every participant accepted the same version. `outcome` holds that proposal. |
| `COMPLETED` | A participant reported the agreed plan as carried out, with evidence. |
| `DECLINED` | A participant declined. One decline ends the task. |
| `EXPIRED` | `expires_at` passed before agreement. |
| `CANCELLED` | The initiator cancelled it. |
| `FAILED` | The system could not continue. |

`COMPLETED`, `DECLINED`, `EXPIRED`, `CANCELLED`, and `FAILED` are terminal. Delivering a message
never completes a task.

A response's `kind` is one of these:

| Kind | Rules |
|---|---|
| `accept` | Accepts `proposal_version`. When every participant has accepted it, the task becomes `AGREED`. |
| `decline` | Ends the task as `DECLINED`. `reason` is optional. |
| `counterpropose` | Needs `proposal`. Makes it the next version and resets everyone else's answer. |
| `complete` | Needs `evidence`, a JSON object such as `{"booking_reference": "TB-39BAD6"}`. Allowed only in `AGREED`. Stored in `outcome.completion` as `{by_user_id, evidence, completed_at}`. |

Every response must name the current `proposal_version`, else `409 stale_version`. The reply is
`{result, approval_id, task}`, where `result` is one of these:

- `applied`: the task changed.
- `noop`: the same answer was already recorded.
- `pending_approval`: the agent's grant requires approval. The answer is parked as an approval
  request until the owner decides.

FCL answers lazily as well as on schedule. A response to a task past `expires_at` fails with
`409 task_expired` and moves the task to `EXPIRED`. A worker job does the same at `expires_at`.
Another job sends a `task.status` reminder halfway to `expires_at` to each participant who has
not answered yet.

## Approvals

An approval request waits for one human, who decides with a user token. An agent token is
rejected, even the approver's own agent's. Requests come in two kinds:

- **`task_response`.** FCL creates it when an agent's `task:respond` grant requires approval.
  The request holds the exact answer. On `grant`, FCL applies that answer. If the task changed
  since the agent answered, the decision fails with `409 approval_superseded` and nothing is
  applied. On `deny`, FCL drops the answer and the agent may answer again.
- **`permission`.** An agent creates it with `POST /v1/approvals` before it acts outside FCL for
  its owner. Only the agent's owner can decide it. FCL records the decision and never performs
  the outside action.

| Scope | `params` |
|---|---|
| `calendar.read` | `{window_start, window_end}`, at most 31 days apart |
| `payment.charge` | `{payee, amount_minor, currency, reference}`. `amount_minor` is in the currency's smallest unit, up to 10,000,000. `currency` is three capital letters. |

A permission request also carries `purpose`, which the owner sees verbatim. An agent should
pass the approval id to the outside service as that service's idempotency key, so an approved
action runs at most once.

After a decision, FCL sends `approval.granted` or `approval.denied` to the requesting agent's
inbox. A decision on an expired request fails with `409 approval_expired`.

## Webhooks

A webhook pushes each inbox delivery to a URL. FCL queues the push in the same transaction that
writes the delivery, and the worker sends it on its next pass. The inbox still holds every
message, so a webhook only adds speed.

FCL sends `POST <url>` with this body:

```json
{"cursor": 7, "message": { "message_id": "msg_...", "type": "coordination.accept", "...": "..." }}
```

These headers come with it:

| Header | Value |
|---|---|
| `FCL-Signature` | `t=<unix seconds>,v1=<hex HMAC-SHA256 of "<t>." + body, keyed with the secret>` |
| `FCL-Delivery` | The delivery cursor |
| `FCL-Message-Id` | The message id |

To verify a delivery, recompute the MAC over the raw body and compare it in constant time.
Also reject a timestamp more than 300 seconds from your clock. `fcl.webhooks.verify` does both:

```python
from fcl.webhooks import verify

ok = verify(secret, request.headers["FCL-Signature"], raw_body, int(time.time()))
```

Any `2xx` answer marks the delivery `delivered`. FCL never follows redirects. Any other answer
or a timeout after 5 seconds is retried with exponential backoff, from about 2 seconds up to 10
minutes, for 8 attempts. After the last attempt the delivery is marked `failed`, and the message
stays readable in the inbox.

Webhook URLs must be `https`. With `--allow-loopback-webhooks`, plain `http` is allowed to
addresses on this machine. [security.md](security.md) explains how FCL vets each destination.
