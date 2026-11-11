#!/usr/bin/env bash
# Starts the "family-orchestrator" Claude Code session. Its only tools are the fcl MCP tools: no built-in tools,
# no claude.ai connectors, no plugins, no user CLAUDE.md, memory, hooks, or skills.
#
#   orchestrator/launch.sh login        # once: sign in inside the dedicated config directory
#   orchestrator/launch.sh              # interactive session with channel push
#   orchestrator/launch.sh -p "..."     # headless, for checks (no channel push in print mode)
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
root="${FCL_ORCH_HOME:-$HOME/.fcl-orchestrator}"
config_dir="$root/claude"
work="$root/work"
mcp="$root/mcp.json"
# Use the real Claude Code binary, not a wrapper script: terminal wrappers (cmux, for one) inject their own
# hooks and settings.
claude_bin="${FCL_CLAUDE_BIN:-}"
if [ -z "$claude_bin" ]; then
  for c in $(type -ap claude); do
    [ "$(head -c 2 "$c")" = "#!" ] || { claude_bin="$c"; break; }
  done
fi
[ -x "$claude_bin" ] || { echo "no Claude Code binary found; set FCL_CLAUDE_BIN" >&2; exit 1; }

umask 077
mkdir -p "$config_dir" "$work"
chmod 700 "$root" "$config_dir" "$work"
install -m 600 "$here/settings.json" "$config_dir/settings.json"
rm -f "$work/CLAUDE.md"

extra=$(find "$work" -mindepth 1 -maxdepth 1 ! -name .claude | head -1)
[ -z "$extra" ] || { echo "refusing: $work must be empty, found $extra" >&2; exit 1; }
if git -C "$work" rev-parse --git-dir >/dev/null 2>&1; then
  echo "refusing: $work is inside a git repository" >&2; exit 1
fi

# A clean environment: nothing from this shell (API keys, cloud credentials, FCL user tokens) reaches the session.
clean_env=(env -i "HOME=$HOME" "USER=${USER:-}" "LOGNAME=${LOGNAME:-}" "PATH=/usr/bin:/bin:$(dirname "$claude_bin")"
  "TERM=${TERM:-xterm-256color}" "LANG=${LANG:-en_US.UTF-8}" "TMPDIR=${TMPDIR:-/tmp}" "COLORTERM=${COLORTERM:-}"
  "CLAUDE_CONFIG_DIR=$config_dir"
  "ENABLE_CLAUDEAI_MCP_SERVERS=false"
  "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1"
  "CLAUDE_CODE_DISABLE_CLAUDE_MDS=1"
  "MCP_TOOL_TIMEOUT=660000")

if [ "${1:-}" = "login" ]; then
  exec "${clean_env[@]}" "$claude_bin" auth login
fi

[ -f "$mcp" ] || { echo "no $mcp: run orchestrator/setup.sh <link-url> first" >&2; exit 1; }

args=(--name family-orchestrator
  --tools ""
  --no-chrome
  --strict-mcp-config --mcp-config "$mcp"
  --permission-mode dontAsk
  --allowedTools "mcp__fcl"
  --append-system-prompt-file "$here/CLAUDE.md")

interactive=1
for a in "$@"; do
  case "$a" in -p|--print) interactive=0 ;; esac
done
# Channels need an interactive session. A custom channel is a research preview, so it loads through the
# development flag, which asks for confirmation at startup.
[ "$interactive" = 1 ] && args+=(--dangerously-load-development-channels server:fcl)

cd "$work"
exec "${clean_env[@]}" "$claude_bin" "${args[@]}" "$@"
