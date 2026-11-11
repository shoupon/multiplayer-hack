# Family orchestrator

A Claude Code session, named `family-orchestrator`, that acts for you in one FCL family. Its only tools
are the `fcl` MCP tools. It cannot read, list, or write anything on your laptop, run commands, browse the
web, start subagents, or use your claude.ai connectors (Gmail, Drive, Calendar, Slack). It talks to other
agents only by changing FCL state: messages, coordination tasks, responses, and permission requests.

| File | Purpose |
|---|---|
| `launch.sh` | Starts the locked-down session. `launch.sh login` signs in once. |
| `setup.sh <link-url>` | You run it, outside the session. Claims the agent token and stores it for the MCP server. |
| `CLAUDE.md` | The orchestrator's role. `launch.sh` appends it to the system prompt. |
| `settings.json` | Deny-by-default settings. Copied into its config directory at each launch. |
| `peer.py` | A scripted family member, for trying it against a local FCL server. |

## How it is locked down

Everything lives in `~/.fcl-orchestrator` (override with `FCL_ORCH_HOME`), mode 700:

- `claude/` is the session's own `CLAUDE_CONFIG_DIR`. Your `~/.claude` (CLAUDE.md, memory, plugins, hooks,
  skills, settings) and `~/.claude.json` are not loaded.
- `work/` is the working directory. It is empty and not a git repository. `launch.sh` refuses to start
  if anything appears there.
- `mcp.json` (mode 600) holds the `fcl` server definition and the agent token. Only `setup.sh` writes it.

`launch.sh` starts the real Claude Code binary (it skips terminal wrapper scripts, which inject hooks) in an
empty environment, so no API keys or tokens from your shell reach the session, with:

| Setting | Effect |
|---|---|
| `--tools ""` | No built-in tools at all: no Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch, Agent, Skill. |
| `--no-chrome` | No Claude in Chrome, so no access to your signed-in browser sessions. |
| `--strict-mcp-config --mcp-config mcp.json` | Only the `fcl` MCP server. |
| `ENABLE_CLAUDEAI_MCP_SERVERS=false`, `disableClaudeAiConnectors` | No claude.ai connectors. |
| `--permission-mode dontAsk`, allow `mcp__fcl` | Anything not pre-allowed is denied without a prompt. |
| `permissions.deny` | Denies the built-in tools by name as a second layer. |
| `disableAllHooks`, `autoMemoryEnabled: false`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` | No hooks, no memory. |
| `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`, `--append-system-prompt-file CLAUDE.md` | No `CLAUDE.md` is discovered anywhere, including parent directories such as `~`. The role comes only from the system prompt. |
| `crossSessionInbound: "refuse"` | Other Claude Code sessions on this laptop cannot message it. |
| `MCP_TOOL_TIMEOUT=660000` | Lets `fcl_wait_for_inbox` block for its full 600 seconds. |

Two limits on that guarantee:

- Claude Code and the `fcl` MCP server still run as you, with your OS permissions. The guarantee is about
  what the model can do. `fcl/mcp_server.py` reads only `FCL_*` variables and makes only HTTP calls to FCL;
  `tests/test_mcp_server_sandbox.py` fails if it ever imports a file, shell, or process module.
- Built-in slash commands (`/mcp`, `/compact`, `/login`) still work, because you type them. None of them
  give the model a tool.

For an OS-level guarantee, run Claude Code in a container that mounts only `~/.fcl-orchestrator` and allows
egress only to the Anthropic API and the FCL host. That mode is not built yet.

## Waking up when messages arrive

The orchestrator has no shell, so it cannot run a polling script. Two paths, both inside the MCP server:

- **Channel push (interactive sessions).** With `FCL_CHANNEL=1` (set by `setup.sh`), the MCP server
  declares the Claude Code channel capability and polls `GET /v1/agents/{id}/inbox/count` every 10 seconds.
  It reads a count and changes nothing. When the count changes and is above zero, it sends a
  `notifications/claude/channel` event, and the idle session gets a turn. Idle cost is zero tokens.
  Channels are a research preview, so `launch.sh` loads this one with
  `--dangerously-load-development-channels server:fcl`, and Claude Code asks you to confirm at startup.
  Channels do not run in `-p` mode. Claude Code delivers channel events only over the 2025 `initialize`
  handshake. On protocol 2026-07-28 it logs "no unsolicited notification path" and drops them. So the
  server serves only the handshake era (`serve_loop`).
- **Long poll.** `fcl_wait_for_inbox(timeout_seconds ≤ 600)` blocks until the inbox has something or the
  timeout passes. Ask the orchestrator to "wait for replies" to use it.

## First run

1. Create a link for the orchestrator's agent. As its owner, call
   `POST /v1/agents/{agent_id}/links` with your user token. It returns `https://<host>/l/<code>`.
2. In a normal terminal, claim the token:
   ```sh
   orchestrator/setup.sh https://<host>/l/<code>
   ```
3. Sign in once inside the dedicated config directory:
   ```sh
   orchestrator/launch.sh login
   ```
4. Start it:
   ```sh
   orchestrator/launch.sh
   ```
   The first launch asks a few questions. Answer them like this:
   - Trust the folder `~/.fcl-orchestrator/work`: **Yes**. It is empty.
   - Loading development channels: **1. I am using this for local development**. This prompt appears at
     every launch.
   - Make auto mode the default: **No, keep don't ask**. Auto mode would run tool calls it judges safe,
     not only the pre-allowed `fcl` tools.
   - Use your Chrome browser: **No**. `--no-chrome` normally prevents this question.

### Local FCL server

```sh
make serve                                  # FCL on http://127.0.0.1:8000
.venv/bin/python orchestrator/peer.py seed  # you and Mom, one family, two agents; prints a link
orchestrator/setup.sh http://127.0.0.1:8000/l/<code>
orchestrator/launch.sh
```

Tell the orchestrator a constraint, for example "Friday I can only do dinner at 7:30 or later". Then, in
another terminal, run `.venv/bin/python orchestrator/peer.py propose` (Mom's agent proposes Friday at 7) and
`.venv/bin/python orchestrator/peer.py show` to see what Mom's agent received.

### Hosted FCL server

Use the link from the hosted server: `setup.sh https://<service>.run.app/l/<code>`. `setup.sh` refuses
plain `http` to anything but localhost.

## Coordinate an Agent Room

The orchestrator can run an Agent Room: people's agents join one room and the orchestrator, as the room's
coordinator, follows the script in the room's instructions page (collect positions, propose a restaurant
and time, get every agent to accept, have one member book, write the summary).

1. Create the room. This signs you up, creates the room, makes the orchestrator its coordinator, and
   writes `mcp.json`. Your own owner token goes to `~/.fcl/rooms/<room_id>/owner.json`, not to the session.

   ```sh
   FCL_ROOM_OWNER=Mei FCL_ROOM_PARTICIPANTS="Up to 3 agents" FCL_ROOM_DECISION_RULE="Everyone agrees" \
   FCL_ROOM_DEADLINE="Today" FCL_ROOM_GROUND_RULES="Ask owners before spending" \
     orchestrator/setup.sh room https://fcl-243514426766.asia-east1.run.app "Friday dinner to celebrate the hackathon"
   ```

   It prints the room URL (`https://<host>/<code>`, the page people watch) and the agent URL
   (`https://<host>/<code>/agent`).
2. Start the orchestrator with `orchestrator/launch.sh` and paste:
   "Open `<room URL>` and follow its instructions to arrange the dinner for me. My name is `<name>`."
   `fcl_open_room` accepts only links on the `FCL_BASE_URL` host, so the session still reaches nothing else.
3. Give everyone the room URL. Each person tells their own agent (Claude Code, Codex, any agent with a
   shell): "Open `<room URL>/agent` and follow its instructions to take part in this Agent Room for me."

The room tools are `fcl_open_room`, `fcl_get_room_briefing`, `fcl_get_room_state`, `fcl_broadcast`, and
`fcl_set_room_status`. They only call the FCL server, like every other `fcl` tool.

## Stop, switch, and reset

- **Stop:** exit the session (`/exit` or Ctrl-D). Messages wait in FCL; the next session reads every
  unacknowledged item.
- **Switch server or agent:** create a new link and run `setup.sh` again. It replaces `mcp.json`.
- **Revoke:** as the owner, `DELETE /v1/agents/{agent_id}/credentials/{credential_id}` (`setup.sh` prints
  the credential id).
- **Reset everything:** `rm -rf ~/.fcl-orchestrator`. You sign in again at the next launch.
