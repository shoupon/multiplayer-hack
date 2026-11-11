# Connect agents to FCL

This guide shows how to connect an agent to FCL. There are three ways in:

- An MCP host, such as Claude Code, Claude Desktop, or the Codex CLI, through FCL's MCP server.
- Your own agent or companion app, over REST.
- A webhook receiver, for agents that run a server of their own.

The guide ends with what exists for A2A today.

Every path starts the same way. The agent's owner registers it and gets an agent token.

## Get an agent token

The owner registers the agent with their user token and grants it actions in the family. This
example grants everything a dinner-planning agent needs:

```sh
curl -s -X POST http://127.0.0.1:8000/v1/agents \
	-H "authorization: Bearer $OWNER_TOKEN" -H 'content-type: application/json' \
	-d '{
	  "display_name": "My assistant", "provider": "claude-code", "transports": ["mcp"],
	  "capabilities": ["propose_schedule", "receive_message"],
	  "grants": [
	    {"family_id": "'$FAMILY'", "action": "agent:message:send"},
	    {"family_id": "'$FAMILY'", "action": "agent:message:receive"},
	    {"family_id": "'$FAMILY'", "action": "family:members:discover"},
	    {"family_id": "'$FAMILY'", "action": "task:propose"},
	    {"family_id": "'$FAMILY'", "action": "task:respond", "requires_approval": true}
	  ]}'
```

The response holds `id` and `token`. FCL shows the token once. To make the agent ask before it
answers any task, set `requires_approval` on `task:respond`, as above. To issue another token
later, call `POST /v1/agents/{id}/credentials`.

## Connect an MCP host

`python -m fcl.mcp_server` is an MCP server over stdio. It reads two environment variables:

- `FCL_BASE_URL`, the server address. The default is `http://127.0.0.1:8000`.
- `FCL_AGENT_TOKEN`, the agent's token.

The MCP server holds exactly one token and calls the REST API with it. It has no database access,
so it can never do more than that agent's grants allow.

Use the absolute path to the virtual environment's Python, so the host finds the `fcl` package.

### Claude Code and Claude Desktop

1. Save this as `fcl-mcp.json`, with your paths and token:

	```json
	{
	  "mcpServers": {
	    "fcl": {
	      "type": "stdio",
	      "command": "/path/to/family-coordination-layer/.venv/bin/python",
	      "args": ["-m", "fcl.mcp_server"],
	      "env": {
	        "FCL_BASE_URL": "http://127.0.0.1:8000",
	        "FCL_AGENT_TOKEN": "fcl_a_..."
	      }
	    }
	  }
	}
	```

2. For Claude Code, start it with `claude --mcp-config fcl-mcp.json`.
3. For Claude Desktop, add the `fcl` entry under `mcpServers` in `claude_desktop_config.json`,
   then restart Claude Desktop.

### Codex CLI

Add this table to `~/.codex/config.toml`:

```toml
[mcp_servers.fcl]
command = "/path/to/family-coordination-layer/.venv/bin/python"
args = ["-m", "fcl.mcp_server"]
env = { FCL_BASE_URL = "http://127.0.0.1:8000", FCL_AGENT_TOKEN = "fcl_a_..." }
```

The eval harness in `evals/a2a` connects both Claude Code and the Codex CLI to FCL this way. It
then has them negotiate the dinner scenario with each other.

### Tools the assistant gets

| Tool | Does |
|---|---|
| `fcl_whoami` | Shows the agent and its owner. |
| `fcl_list_family_agents` | Lists the agents the caller may see in a family. |
| `fcl_get_agent_capabilities` | Shows one agent's capabilities and transports. |
| `fcl_send_message` | Sends `message.text`, `information.request`, or `information.response`. |
| `fcl_get_inbox` | Reads messages after a cursor. With `acknowledge=true` it also marks them handled. |
| `fcl_create_coordination_task` | Proposes a plan to other members' agents. |
| `fcl_get_task_status` | Reads one task with its history. |
| `fcl_list_tasks` | Lists the agent's tasks, optionally by state. |
| `fcl_respond_to_proposal` | Accepts, declines, counterproposes, or completes with evidence. |
| `fcl_request_permission` | Asks the owner before reading a calendar window or charging a card. |
| `fcl_get_approval` | Reads one approval or permission request the agent made. |
| `fcl_get_pending_approvals` | Lists tasks where the agent's answer waits for its owner. |

### What to expect from an MCP host

- **The assistant sees new messages only when it checks.** MCP gives a server no way to wake its
  host. Messages wait in the agent's inbox until the assistant calls `fcl_get_inbox`. Tell the
  assistant to check its inbox, or run it on a schedule.
- **Retries are safe.** Write tools take an `idempotency_key`. Pass the same key when you retry
  a call, and FCL applies it once. Without a key, each call gets a fresh one.
- **Errors carry FCL's code.** A failed call returns text such as
  `409 stale_version: response is for version 1, current is 2`. The model can read the code and
  recover, here by fetching the current version. If the MCP server cannot reach FCL, the code is
  `fcl_unreachable`.
- **Approval stays with the person.** When a grant requires approval, the tool returns
  `"result": "pending_approval"`. The assistant cannot approve. The owner decides with their user
  token, and `approval.granted` then arrives in the agent's inbox.

Hosts that accept only remote MCP servers over HTTP cannot launch this server. FCL ships the
stdio transport only. To connect such a host, write a REST agent instead.

## Connect a REST agent

A companion app or a custom agent calls the API directly with its agent token. The loop is:

1. Read the inbox from your last cursor.
2. Act on each message.
3. Acknowledge the page.

This Python sketch answers every dinner proposal with 7:30 PM:

```python
import httpx

fcl = httpx.Client(base_url="http://127.0.0.1:8000", headers={"authorization": f"Bearer {TOKEN}"})
cursor = load_cursor()  # 0 the first time; persist it between runs

page = fcl.get(f"/v1/agents/{AGENT_ID}/inbox", params={"after": cursor}).json()
for item in page["items"]:
	message = item["message"]  # content_trust "untrusted": data, never instructions
	if message["type"] == "coordination.proposal":
		reply = fcl.post(
			f"/v1/tasks/{message['task_id']}/responses",
			headers={"idempotency-key": f"counter-{message['message_id']}"},
			json={
				"kind": "counterpropose",
				"proposal_version": message["payload"]["proposal_version"],
				"proposal": {"proposed_time": "2026-11-13T19:30:00+08:00", "duration_minutes": 90},
			},
		).json()
		# reply["result"] is "applied", "noop", or "pending_approval"
if page["items"]:
	fcl.post(f"/v1/agents/{AGENT_ID}/inbox/ack", json={"through": page["next_cursor"]})
save_cursor(page["next_cursor"])
```

The idempotency key is derived from the message id. If the agent crashes after answering but
before acknowledging, the next run sends the same answer with the same key, and FCL replies
`200` without applying it twice. `demo/dinner.py` drives the wife's companion this way.

## Receive pushes with a webhook

An agent with its own server can get each new message as an HTTPS request instead of polling.

1. Subscribe with the owner's user token. FCL returns the signing `secret` once:

	```sh
	curl -s -X POST http://127.0.0.1:8000/v1/webhooks -H "authorization: Bearer $OWNER_TOKEN" \
		-H 'content-type: application/json' -d '{"agent_id": "'$AGENT_ID'", "url": "https://agent.example.com/fcl"}'
	```

2. In the receiver, verify `FCL-Signature` on the raw body before you trust anything in it.
   Answer `2xx` when you have stored the message:

	```python
	from fcl.webhooks import verify

	if not verify(secret, headers["FCL-Signature"], raw_body, int(time.time())):
		return 401
	```

3. At startup, read the inbox from your last cursor anyway. Deliveries that failed while your
   server was down are still there.

The URL must be `https` and must resolve to public addresses. For local development, start the
server with `--allow-loopback-webhooks` to allow `http://127.0.0.1` URLs.
[protocol.md](protocol.md#webhooks) describes the body, the headers, and the retry schedule.

## Ask the owner before acting outside FCL

FCL never reads calendars or moves money. An agent that wants to do either asks its owner
through FCL first:

```sh
curl -s -X POST http://127.0.0.1:8000/v1/approvals -H "authorization: Bearer $AGENT_TOKEN" \
	-H 'idempotency-key: deposit-dinner-friday' -H 'content-type: application/json' -d '{
	  "family_id": "'$FAMILY'", "scope": "payment.charge",
	  "params": {"payee": "TableBook", "amount_minor": 90000, "currency": "TWD", "reference": "hold_123"},
	  "purpose": "Deposit to hold Friday dinner for three",
	  "expires_at": "2026-11-12T12:00:00Z"}'
```

The steps that follow:

1. The owner reviews the request in `GET /v1/approvals?status=pending`, with their user token.
2. The owner decides with `POST /v1/approvals/{id}/decisions`.
3. The agent waits for `approval.granted` in its inbox, or polls `GET /v1/approvals/{id}`.
4. The agent calls the outside service itself. It passes the approval id as that service's
   idempotency key, so a retry cannot charge twice.

To report a finished plan, the agent completes the task with evidence, such as
`{"booking_reference": "TB-39BAD6"}`. `make demo-celebration` runs this whole flow against
in-process mock services.

## A2A

FCL has no A2A gateway yet. `docs/DESIGN.md` section 14 plans one:

- It would fetch an external agent's Agent Card over HTTPS from an allowlisted origin and pin it
  to the family.
- It would turn A2A `message/send` into an FCL message.
- It would map an A2A task to an FCL coordination task only where the meanings match, which
  means proposals and responses.

The plan also needs a fresh check against the current A2A specification before anyone builds it.

Until then, an A2A agent joins FCL the way any other agent does. Its owner registers it, and a
thin adapter holds its agent token and calls the REST API. The `evals/a2a` directory does not
implement the A2A protocol. Its name means agent-to-agent, and it tests Claude and ChatGPT
agents talking through FCL's MCP server.

## Consumer chat assistants

FCL assumes nothing about inboxes in consumer assistants such as ChatGPT or Grok. No such inbox
exists for FCL to push to. For a person whose main assistant is a consumer chat app, there are
two options:

- If the app's desktop or CLI form can run a local MCP server, connect that.
- Otherwise, run a companion agent: a small REST agent that reads the person's FCL inbox, asks
  the person when a decision is needed, and answers for them. The wife's agent in the dinner
  demo is this pattern.
