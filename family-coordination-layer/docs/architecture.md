# About the FCL architecture

FCL has to do one job well: let agents made by different companies agree on a plan for a family.
It has to do that without trusting any of those agents, and it has to run on one laptop. Those
two constraints explain most of the design. This page walks through the parts and the reasons
for each.

## One process, one SQLite file

```text
 MCP host ──stdio──> python -m fcl.mcp_server ──HTTP + agent token──┐
 Custom agent ───────────────HTTP + agent token─────────────────────┤
 Person or companion app ────HTTP + user token──────────────────────┤
                                                                    v
                ┌────────────── python -m fcl serve ──────────────────────┐
                │ api.py       authenticate, parse, map errors            │
                │ services     families, agents, grants, messaging,       │
                │              tasks, approvals, permissions, webhooks    │
                │ cores        policy.authorize, machine.apply (pure)     │
                │ worker       expiry, reminders, webhook delivery        │
                └────────────────────────┬────────────────────────────────┘
                                         │ one transaction per command
                                         v
                ┌──────────── .data/fcl.db (SQLite, WAL) ─────────────────┐
                │ domain tables, message_deliveries, scheduled_jobs       │
                └─────────────────────────────────────────────────────────┘
```

The API and the job worker share one process and one database file. There is no message
broker, no cache, and no second service. `make install && make serve` is the whole deployment.

SQLite allows one writer at a time, and FCL leans into that. Every command opens a
`BEGIN IMMEDIATE` transaction, so commands run one after another. Two agents answering the same
task at the same moment cannot interleave. The second one sees the first one's result and gets
`409 stale_version` if it answered an old version. FCL gets that ordering from the database lock
instead of from optimistic retries or row locks. One family produces a handful of writes per
minute, so serial writes cost nothing. WAL mode lets reads continue during a write.

`fcl/db.py` sets `PRAGMA foreign_keys = ON` on every connection, because SQLite leaves foreign
keys off by default. It applies migrations one statement at a time. `executescript` would commit
any open transaction, so a failed migration would leave half a schema behind.

## Pure cores, thin services

Two modules hold the rules that matter most, and neither touches the database:

- `policy.authorize(question)` decides whether an agent may take an action. The input is the
  actor, the family's active members, the agent's grants, and the time. The output is
  `Allow(grant)` or `Deny(reason)`.
- `machine.apply(task, event, now)` moves a coordination task from one state to the next. The
  output is `Ok(task)` or `Err(code)`.

A service such as `tasks.respond` does the rest. It loads a snapshot inside the transaction,
asks the policy, applies one event, and saves the result. It writes a row to `task_transitions`
and queues the messages that announce the change. If any step fails, the transaction rolls back
and the task is as it was. Keeping the rules pure makes them small enough to read in one
sitting, and the tests exercise them without a server.

The task state is derived, not set by hand. Each participant row stores whether that person has
the proposal, whether their answer waits for approval, and which version they accepted.
`machine._settle` computes the open state from those facts. It returns `AWAITING_APPROVAL` if any
answer is parked, and `COUNTERPROPOSED` when the version is above 1. If an invitee has not
received the proposal yet, it returns `AWAITING_DELIVERY`, and otherwise `AWAITING_RESPONSE`. The
task-level state can never disagree with the participant data, because it is computed from it.

## Only the engine speaks for a task

Agents send three message types themselves: `message.text`, `information.request`, and
`information.response`. Every other type, such as `coordination.proposal` and
`approval.granted`, has one writer, the engine that changes the state. An agent cannot send a
message that claims a task was accepted. It can only call `POST /v1/tasks/{id}/responses`, and
the announcement follows from the state change. `envelope.MESSAGE_TYPES` records the origin of
each type, and `POST /v1/messages` rejects engine types with `422 engine_only_type`.

## The inbox is the delivery model

Consumer assistants have no address FCL can push to, and MCP gives a server no way to wake its
host. So FCL never relies on pushing. Every message lands in `message_deliveries`, a durable
inbox per agent with a cursor that only increases. An agent reads from its last cursor whenever
it runs. If it crashes before acknowledging, it reads the same messages again.

A webhook is an addition, not a replacement. It is useful for an agent with a server of its
own, like the companion in the dinner demo. When a delivery is written, FCL queues a webhook job
in the same transaction. If the push never succeeds, the message is still in the inbox.

## Side effects go through an outbox

Anything that happens after a commit is written as a row in `scheduled_jobs` inside that commit.
This covers task expiry, reminders, and webhook pushes. The worker thread claims due jobs with a
two-minute lease, so a job that crashed mid-run becomes claimable again. Failed jobs retry with
exponential backoff and become dead after `max_attempts`. A `dedupe_key` stops the same job from
being queued twice.

This arrangement is why the dinner demo can `SIGKILL` the server and restart it without losing
anything. Before the commit, nothing happened. After the commit, the job row is on disk and the
next worker pass runs it.

## Retries are safe by construction

Agents retry, MCP hosts retry, and networks drop responses. Every command that creates something
takes an `Idempotency-Key`. FCL stores the key with the thing it created rather than in a
separate cache, under a unique constraint per agent:

- `messages`: `(sender_agent_id, idempotency_key)`
- `coordination_tasks`: `(initiator_agent_id, idempotency_key)`
- `task_transitions`: `(task_id, actor_agent_id, idempotency_key)`, for answers
- `approval_requests`: `(requesting_agent_id, idempotency_key)`, for permission requests

A replay finds the original row and returns it.
A replay with a different body fails, because each row also stores a hash of the request.
Answers to tasks carry `proposal_version`, so a late retry of an old answer cannot apply to a
newer proposal.

## One approval queue, two kinds of request

People should see everything their agents are waiting on in one place. A parked task answer
(`task_response`) and a request to act outside FCL (`permission`) share the `approval_requests`
table, the `GET /v1/approvals` list, and the decision endpoint. They differ in what a grant does:

- Granting a parked answer applies it to the task, if the task has not moved on since.
- Granting a permission only records the owner's yes and tells the agent. FCL never reads a
  calendar or charges a card. The agent does that itself and uses the approval id as the
  outside service's idempotency key.

## Where things live

| Module | Owns |
|---|---|
| `api.py` | The only FastAPI module. Request models, auth dependencies, error mapping, body and rate limits. |
| `auth.py`, `tokens.py`, `identity.py` | Tokens, principals, users |
| `families.py`, `agents.py`, `grants.py` | Membership, agent registration, discovery, grants |
| `policy.py` | Pure authorization |
| `machine.py`, `proposal.py` | Pure task state machine and the proposal model |
| `envelope.py`, `messaging.py` | Message types, sending, inbox, acknowledgement |
| `tasks.py`, `approvals.py`, `permissions.py` | Task commands, approval decisions, permission requests |
| `jobs.py`, `worker.py`, `webhooks.py` | Outbox, worker thread, signed delivery |
| `audit.py` | Append-only audit events, including denials |
| `mcp_server.py` | MCP tools over the REST API, holding one agent token |
| `db.py`, `migrations/` | Connections, transactions, schema |

## What would change for a hosted service

This design fits one household on one machine. A service hosting many families would change a
few deliberate choices:

- PostgreSQL with row-level security would replace SQLite. Composite foreign keys already carry
  `family_id` everywhere, so the port is mostly mechanical. `db.py` keeps SQL out of FastAPI for
  that reason.
- Real sign-in would replace open signup.
- A key management service would hold the server key instead of a file.
- The rate limiter would move out of process memory.

None of these would change the protocol that agents see.
