#!/usr/bin/env bash
# Stores an FCL agent token in the orchestrator's MCP config. Run this yourself, in a normal terminal.
# The orchestrator session never sees the link, the room's owner token, or its own token.
#
#   orchestrator/setup.sh https://<fcl-host>/l/<code>
#       Claim the token behind a monitor link (the orchestrator acts for you in an existing family).
#
#   orchestrator/setup.sh room https://<fcl-host> "<goal>"
#       Create an Agent Room for the goal and make the orchestrator its coordinator. Prints the room URL to
#       share. Optional brief: FCL_ROOM_PARTICIPANTS, FCL_ROOM_DECISION_RULE, FCL_ROOM_DEADLINE,
#       FCL_ROOM_GROUND_RULES. FCL_ROOM_OWNER is your display name (default: your login name).
#
#   orchestrator/setup.sh room https://<fcl-host>/<code> <owner-file.json>
#       Make the orchestrator the coordinator of a room you already created. <owner-file.json> holds the room
#       creator's user token as "user_token" or "host_token".
set -euo pipefail

usage() {
  echo "usage: $0 <link-url>                      (from POST /v1/agents/{id}/links)" >&2
  echo "       $0 room <base-url> \"<goal>\"        (create a room; the orchestrator coordinates it)" >&2
  echo "       $0 room <room-url> <owner-file.json> (coordinate a room you created)" >&2
  exit 2
}

mode=link
if [ "${1:-}" = room ]; then
  [ $# -eq 3 ] || usage
  mode=room
else
  [ $# -eq 1 ] || usage
fi

here=$(cd "$(dirname "$0")" && pwd)
fcl_dir=$(dirname "$here")
py="$fcl_dir/.venv/bin/python"
root="${FCL_ORCH_HOME:-$HOME/.fcl-orchestrator}"

[ -x "$py" ] || { echo "missing $py; run 'make install' in $fcl_dir first" >&2; exit 1; }

umask 077
mkdir -p "$root"
chmod 700 "$root"

FCL_MODE="$mode" FCL_ARG1="${2:-$1}" FCL_ARG2="${3:-}" FCL_MCP_CONFIG="$root/mcp.json" FCL_PY="$py" \
FCL_OWNER_NAME="${FCL_ROOM_OWNER:-${USER:-owner}}" "$py" -I - <<'PY'
import json, os, re, sys
import httpx

def https_base(url: str) -> str:
    if url.startswith("http://") and not re.match(r"http://(127\.0\.0\.1|localhost)(:\d+)?$", url):
        sys.exit("refusing plain http to a non-local host; use the https URL")
    return url

def ok(r: httpx.Response, what: str) -> dict:
    if r.status_code not in (200, 201):
        sys.exit(f"{what} failed: {r.status_code} {r.text}")
    return r.json()

env_extra = {}
if os.environ["FCL_MODE"] == "link":
    link = os.environ["FCL_ARG1"].rstrip("/")
    m = re.fullmatch(r"(https?://[^/]+)/l/([A-Za-z0-9_-]{32})", link)
    if not m:
        sys.exit("not a monitor link: expected https://<host>/l/<32-char code>")
    base_url = https_base(m.group(1))
    claim = ok(httpx.post(f"{link}/claim", timeout=15), "claim")
    token, agent_id = claim["token"], claim["agent_id"]
    print(f"claimed agent {agent_id} (credential {claim['credential_id']}) on {base_url}")
elif re.fullmatch(r"https?://[^/]+/(r/)?[A-Za-z0-9_-]{32}(/agent)?/?", os.environ["FCL_ARG1"]):
    m = re.fullmatch(r"(https?://[^/]+)/(?:r/)?([A-Za-z0-9_-]{32})(?:/agent)?/?", os.environ["FCL_ARG1"])
    base_url, code = https_base(m.group(1)), m.group(2)
    with open(os.environ["FCL_ARG2"]) as f:
        owner_file = json.load(f)
    owner_token = owner_file.get("user_token") or owner_file.get("host_token")
    if not owner_token:
        sys.exit("the owner file has no user_token or host_token")
    coord = ok(httpx.post(f"{base_url}/{code}/coordinator", headers={"authorization": f"Bearer {owner_token}"},
                          timeout=15), "add coordinator")
    token, agent_id = coord["token"], coord["agent_id"]
    env_extra["FCL_ROOM_CODE"] = code
    print(f"room       {base_url}/{code}")
    print(f"for agents {base_url}/{code}/agent")
    print(f"coordinator agent {agent_id}")
else:
    base_url = https_base(os.environ["FCL_ARG1"].rstrip("/"))
    if not re.fullmatch(r"https?://[A-Za-z0-9.-]+(:\d+)?", base_url):
        sys.exit("expected a base URL such as https://fcl-243514426766.asia-east1.run.app")
    brief = {k: os.environ[f"FCL_ROOM_{k.upper()}"] for k in ("participants", "decision_rule", "deadline", "ground_rules")
             if os.environ.get(f"FCL_ROOM_{k.upper()}")}
    c = httpx.Client(base_url=base_url, timeout=15)
    me = ok(c.post("/v1/users", json={"display_name": os.environ["FCL_OWNER_NAME"]}), "sign up")
    owner = {"authorization": f"Bearer {me['token']}"}
    room = ok(c.post("/v1/rooms", headers=owner, json={"name": os.environ["FCL_ARG2"], "brief": brief}), "create room")
    code = room["room_url"].rsplit("/", 1)[1]
    coord = ok(c.post(f"/{code}/coordinator", headers=owner), "add coordinator")
    token, agent_id = coord["token"], coord["agent_id"]
    env_extra["FCL_ROOM_CODE"] = code
    # Your owner token for this room: approvals and anything only the room's creator may do. Not the orchestrator's.
    owner_dir = os.path.join(os.path.expanduser("~"), ".fcl", "rooms", room["id"])
    os.makedirs(owner_dir, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(owner_dir, "owner.json"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"base_url": base_url, "room_id": room["id"], "user_id": me["id"],
                   "user_token": me["token"], "url": room["room_url"]}, f, indent=2)
    print(f"room       {room['room_url']}")
    print(f"for agents {room['agent_url']}")
    print(f"coordinator agent {agent_id}; your owner token is in {owner_dir}/owner.json")

config = {"mcpServers": {"fcl": {
    "type": "stdio",
    "command": os.environ["FCL_PY"],
    "args": ["-I", "-m", "fcl.mcp_server"],
    "env": {"FCL_BASE_URL": base_url, "FCL_AGENT_TOKEN": token, "FCL_CHANNEL": "1", **env_extra},
}}}
path = os.environ["FCL_MCP_CONFIG"]
tmp = f"{path}.tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(config, f, indent=2)
os.replace(tmp, path)
print(f"token stored in {path} (mode 600)")
PY
