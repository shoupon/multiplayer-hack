# Family Coordination Layer

FCL lets the personal AI agents of different family members coordinate with each other,
whichever company makes each agent. One agent proposes dinner on Friday at 7 PM. Another agent
counterproposes 7:30. Its human approves, and both people see the same agreed plan.

FCL is a small server. It owns identity, family membership, permissions, durable message
delivery, and the state of each coordination task. Agents talk to it over REST or through an
MCP server. Authorization is plain code over stored grants. No language model ever decides
what an agent may do.

Everything in this directory runs on one laptop. It needs Python 3.10 or later and a SQLite
file, with no other services.

## Run it

```sh
cd family-coordination-layer
make install     # creates .venv and installs fcl with its dev tools
make check       # runs the test suite
make demo        # the dinner scenario, end to end, against a real server process
```

`make demo` starts `python -m fcl serve` on a free port with a fresh database under `.data/`.
It then plays both sides of the dinner scenario:

1. The husband's agent connects through the MCP server and proposes Friday at 7 PM. It retries
   the call with the same idempotency key and still gets one task.
2. The demo kills the FCL process with `SIGKILL` and restarts it on the same database. The
   proposal survives.
3. The wife's companion agent reads its inbox over REST and counterproposes 7:30 PM. Her grant
   requires approval, so the answer waits for her.
4. The wife approves that exact counterproposal with her own user token.
5. The husband's agent reads the counterproposal over MCP and accepts it. Both people read
   `AGREED` at 19:30.

The demo exits non-zero if any step goes wrong.

Two more demos build on the same server:

| Command | What it shows |
|---|---|
| `make viz` | A page at http://127.0.0.1:8001. Pick a scenario, either the two-agent dinner or the three-agent celebration, and click **Run live demo**. The page replays the run step by step: a sequence diagram of every REST call, MCP tool call, webhook push, and crash, next to the task's state machine and each request and response. In the celebration, calls to the mock calendars, TableBook, and email appear as arrows to an Outside world lane. Open http://127.0.0.1:8001/#celebration to start on it. |
| `make demo-celebration` | Three people's agents plan a celebration dinner. They exchange calendars and dietary needs, and the concierge books a restaurant. Each agent asks its owner before it reads a calendar or pays a deposit. The calendars, booking service, and email are in-process mocks. Add `ARGS=--interactive` to make the owners' decisions yourself. |

## Run the server yourself

```sh
make serve                                   # http://127.0.0.1:8000, database at .data/fcl.db
.venv/bin/python -m fcl serve --port 9000 --db /tmp/fcl.db
.venv/bin/python -m fcl migrate --db /tmp/fcl.db
```

The server applies migrations on start and runs a background worker in the same process. The
worker expires tasks, sends reminders, and delivers webhooks. FastAPI serves the generated API
reference at http://127.0.0.1:8000/docs.

Settings come from flags or environment variables:

| Variable | Flag | Default | Meaning |
|---|---|---|---|
| `FCL_DB_PATH` | `--db` | `.data/fcl.db` | SQLite file. Created on first start. |
| `FCL_HOST` | `--host` | `127.0.0.1` | Bind address. Any other value prints a warning, because signup is open. |
| `FCL_PORT` | `--port` | `8000` | Port. |
| `FCL_SERVER_KEY` | | `server.key` next to the database | Base64 key that webhook signing secrets derive from. If unset, the server creates a 32-byte key file with mode `0600`. |
| `FCL_ALLOW_LOOPBACK_WEBHOOKS` | `--allow-loopback-webhooks` | `false` | Allows `http://` webhook URLs that resolve to this machine. For local development only. |
| `FCL_MAX_NEGOTIATIONS` | | `5` | Counterproposals allowed per task. |
| `FCL_MAX_TASK_TTL_HOURS` | | `336` | Latest allowed `expires_at` for a task, in hours from now. |
| `FCL_WORKER_INTERVAL_SECONDS` | | `1.0` | How often the worker looks for due jobs. |
| `FCL_RUN_WORKER` | | `true` | Set `false` to run the API without the worker. |
| `FCL_RATE_LIMIT_PER_MINUTE` | | `600` | Requests per minute per credential. |

## Walk through the API with curl

This is the dinner scenario as raw HTTP requests. It needs `curl` and `jq` and a server on port
8000. Every command reuses shell variables from the one before it.

Create two people and a family. `POST /v1/users` is open in this MVP and returns the user's
token once:

```sh
FCL=http://127.0.0.1:8000
HUSBAND=$(curl -s -X POST $FCL/v1/users -H 'content-type: application/json' \
	-d '{"display_name": "Husband"}' | jq -r .token)
WIFE=$(curl -s -X POST $FCL/v1/users -H 'content-type: application/json' \
	-d '{"display_name": "Wife"}' | jq -r .token)

FAMILY=$(curl -s -X POST $FCL/v1/families -H "authorization: Bearer $HUSBAND" \
	-H 'content-type: application/json' -d '{"name": "The Lins"}' | jq -r .id)
CODE=$(curl -s -X POST $FCL/v1/families/$FAMILY/invitations -H "authorization: Bearer $HUSBAND" \
	-H 'content-type: application/json' -d '{}' | jq -r .code)
curl -s -X POST $FCL/v1/invitations/accept -H "authorization: Bearer $WIFE" \
	-H 'content-type: application/json' -d "{\"code\": \"$CODE\"}" | jq '{family_id, role}'
```

Each person registers an agent and grants it actions in the family. The wife's grant for
`task:respond` requires her approval:

```sh
ALL='["agent:message:send", "agent:message:receive", "family:members:discover", "task:propose", "task:respond"]'
H_AGENT=$(curl -s -X POST $FCL/v1/agents -H "authorization: Bearer $HUSBAND" -H 'content-type: application/json' \
	-d "{\"display_name\": \"Husband's assistant\", \"provider\": \"custom-mcp\", \"transports\": [\"mcp\"],
	     \"grants\": $(jq -c --arg f $FAMILY 'map({family_id: $f, action: .})' <<<"$ALL")}")
W_AGENT=$(curl -s -X POST $FCL/v1/agents -H "authorization: Bearer $WIFE" -H 'content-type: application/json' \
	-d "{\"display_name\": \"Wife's companion\", \"provider\": \"fcl-companion\", \"transports\": [\"inbox\"],
	     \"grants\": $(jq -c --arg f $FAMILY 'map({family_id: $f, action: ., requires_approval: (. == "task:respond")})' <<<"$ALL")}")
H_ID=$(jq -r .id <<<"$H_AGENT"); H_TOKEN=$(jq -r .token <<<"$H_AGENT")
W_ID=$(jq -r .id <<<"$W_AGENT"); W_TOKEN=$(jq -r .token <<<"$W_AGENT")
```

The husband's agent proposes dinner. `Idempotency-Key` is required, and a retry with the same
key returns the same task:

```sh
TASK=$(curl -s -X POST $FCL/v1/tasks -H "authorization: Bearer $H_TOKEN" -H 'idempotency-key: dinner-friday' \
	-H 'content-type: application/json' -d "{
	  \"family_id\": \"$FAMILY\", \"invitees\": [\"$W_ID\"], \"intent\": \"schedule_dinner\",
	  \"proposal\": {\"proposed_time\": \"2026-11-13T19:00:00+08:00\", \"duration_minutes\": 90},
	  \"expires_at\": \"$(date -u -v+12H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '+12 hours' +%Y-%m-%dT%H:%M:%SZ)\"}" | jq -r .id)
```

The wife's companion reads its inbox and counterproposes 7:30 PM. The answer waits for her
approval:

```sh
curl -s "$FCL/v1/agents/$W_ID/inbox?after=0" -H "authorization: Bearer $W_TOKEN" \
	| jq '.items[] | {cursor, type: .message.type, content_trust: .message.content_trust, proposal: .message.payload.proposal}'
curl -s -X POST $FCL/v1/tasks/$TASK/responses -H "authorization: Bearer $W_TOKEN" -H 'idempotency-key: counter-730' \
	-H 'content-type: application/json' \
	-d '{"kind": "counterpropose", "proposal_version": 1,
	     "proposal": {"proposed_time": "2026-11-13T19:30:00+08:00", "duration_minutes": 90}}' \
	| jq '{result, state: .task.state}'
```

```json
{ "result": "pending_approval", "state": "AWAITING_APPROVAL" }
```

The wife approves with her user token. An agent token cannot approve, even her own agent's:

```sh
APPROVAL=$(curl -s "$FCL/v1/approvals?status=pending" -H "authorization: Bearer $WIFE" | jq -r '.items[0].id')
curl -s -X POST $FCL/v1/approvals/$APPROVAL/decisions -H "authorization: Bearer $WIFE" \
	-H 'content-type: application/json' -d '{"decision": "grant"}' | jq '{status, task: .task.state}'
```

The husband's agent accepts version 2, and both people read the outcome:

```sh
curl -s -X POST $FCL/v1/tasks/$TASK/responses -H "authorization: Bearer $H_TOKEN" -H 'idempotency-key: accept-v2' \
	-H 'content-type: application/json' -d '{"kind": "accept", "proposal_version": 2}' | jq '{result, state: .task.state}'
curl -s $FCL/v1/tasks/$TASK -H "authorization: Bearer $WIFE" | jq '{state, outcome}'
```

An answer to an outdated version fails with a code the agent can act on:

```sh
curl -s -X POST $FCL/v1/tasks/$TASK/responses -H "authorization: Bearer $H_TOKEN" -H 'idempotency-key: late' \
	-H 'content-type: application/json' -d '{"kind": "accept", "proposal_version": 1}'
```

```json
{"error":{"code":"stale_version","message":"response is for version 1, current is 2"}}
```

## Connect an agent

An agent needs an agent token, which its owner gets from `POST /v1/agents`. The agent then uses
one of two paths:

- **MCP.** Hosts that run local MCP servers, such as Claude Code, Claude Desktop, and the Codex
  CLI, start `python -m fcl.mcp_server` with `FCL_BASE_URL` and `FCL_AGENT_TOKEN`. The
  assistant gets tools such as `fcl_create_coordination_task` and `fcl_get_inbox`.
- **REST.** A custom agent or companion app calls the API with the token. It reads its inbox
  or subscribes a signed webhook.

The setup for each path is in [docs/integrations.md](docs/integrations.md). That doc also
covers what A2A support exists today, which is none yet.

## Read more

| Doc | What it covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | How the server is put together and why: one process, one SQLite file, a pure state machine, and a transactional outbox. |
| [docs/protocol.md](docs/protocol.md) | Reference for every endpoint, the message envelope, message types, task states, approvals, errors, and webhooks. |
| [docs/security.md](docs/security.md) | The threat model, how authentication and authorization work, and what this MVP does not protect against yet. |
| [docs/integrations.md](docs/integrations.md) | How to connect MCP hosts, REST agents, and webhook receivers. |
| [../docs/DESIGN.md](../docs/DESIGN.md) | The original design, with decisions that still need the owner's call. |

## Layout

```text
fcl/                   the server package
  api.py               HTTP boundary: request models, auth dependencies, routes
  policy.py            pure authorization over grants
  machine.py           pure coordination-task state machine
  envelope.py          message types and payload schemas
  tasks.py approvals.py permissions.py messaging.py   the services
  jobs.py worker.py webhooks.py                       outbox, worker, signed delivery
  mcp_server.py        MCP stdio server: one agent token, REST calls only
  migrations/          SQL migrations, applied in order on start
demo/                  dinner.py, viz.py, celebration.py and their helpers
tests/                 pytest suite
docs/                  architecture, protocol, security, integrations
```

## Limits of this MVP

- `POST /v1/users` needs no credentials. Keep the server on `127.0.0.1`.
- Provider labels such as `custom-mcp` are self-reported and never verified.
- There is no A2A gateway yet.
- Consumer chat assistants expose no inbox FCL can push to. An agent sees new messages only
  when it calls `fcl_get_inbox`, or when its own receiver gets a webhook.
- The rate limiter keeps its counts in memory and resets on restart.
