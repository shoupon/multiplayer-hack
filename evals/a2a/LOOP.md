# Prompt iteration loop

This procedure tunes the prompts that let a Claude agent and a ChatGPT agent coordinate through
FCL. A Claude Code session runs one iteration per wake-up. Start it from this directory with:

```text
/loop Run one iteration of evals/a2a/LOOP.md
```

Each iteration changes one prompt surface, measures it against the baseline on the full scenario
matrix, and keeps the change only if `compare_cli` accepts it. `results.tsv` and `LOOP_LOG.md`
carry the state between iterations and sessions, so any session can resume.

## Rules

- The session judges and edits. Never call an LLM API to judge transcripts or rewrite prompts.
  The only paid calls are the two simulated agents inside `a2a_eval.run`.
- Never weaken a check or a scenario script to make a prompt pass. Change a check only when the
  transcript shows that the check itself misjudged the evidence, and re-baseline after it.
- Prompts stay general. `tests/test_prompts.py` fails when a prompt names the scenarios' people,
  times, or private facts.
- One hypothesis per iteration, one prompt surface per hypothesis.

## Preflight (every iteration, stop at the first failure)

1. FCL is built. `$FCL_DIR/fcl/mcp_server.py` exists and `.venv/bin/python -m pytest -q` passes
   in `$FCL_DIR`. If not, log "waiting on FCL" and schedule the next wake-up in 1800 seconds.
2. FCL is serving at `$FCL_BASE_URL`. `curl -s -o /dev/null -w '%{http_code}' $FCL_BASE_URL/v1/me`
   prints `401`. If not, start it with `make serve` in `$FCL_DIR`, in the background.
3. Both API keys are set, in the environment or in `.env.local`. Check names only. Never print a
   value. If one is missing, stop and ask the owner.
4. `.venv/bin/python -m pytest -q` passes.
5. Spend. Sum the `usd` column of `results.tsv` for rows with a `cand-` or `baseline` label.
   Stop when the total reaches `LOOP_BUDGET_USD` (default 60). Cost per matrix run was estimated
   on 2026-11-11 at $5 to $9 for 16 episodes. Each run is also capped by `MAX_RUN_USD`.

## Iteration

1. **Baseline.** If no run in `results.tsv` has the current prompt hash
   (`python -m a2a_eval.run --print-prompts | tail -1`), run
   `.venv/bin/python -m a2a_eval.run --reps 2 --label baseline` and record its run id.
   The first run against a new FCL build may fail with "FCL contract mismatch". Fix the request
   shapes in `a2a_eval/fcl.py` against the real API, then rerun.
2. **Diagnose.** Read the failing episodes in `runs/<run_id>/episodes.jsonl`. Hand the bulk
   reading to a subagent that returns failure clusters: the failed check, the provider in each
   role, the turn where it went wrong, and the quoted tool calls or reply that show it.
   Attribute each cluster to one cause:
   - **Prompt.** The agent had what it needed and acted wrongly. This loop fixes it.
   - **FCL.** An unclear error, a missing tool argument, or a wrong state. Log it in
     `LOOP_LOG.md` for the FCL owner. Do not paper over it with prompt text.
   - **Harness or scenario.** A check misjudged the evidence, or a scripted line is ambiguous.
     Fix it, log it, and re-baseline.
3. **Hypothesis.** Pick the largest prompt-caused cluster. Choose the narrowest surface, in this
   order: a tool description in `prompts/tools.json`, then `prompts/shared.md`, then a provider
   overlay. Use `claude.md` or `openai.md` only when the failure follows one provider across
   both role orientations. Append the iteration to `LOOP_LOG.md` before running: number, cluster,
   hypothesis, file changed.
4. **Candidate.** Run `.venv/bin/python -m a2a_eval.run --reps 2 --label cand-<n>` with the same
   scenarios and pairs as the baseline.
5. **Decide.** Run `.venv/bin/python -m a2a_eval.compare_cli <baseline_run_id> latest`.
   - Exit 0 (ACCEPT): commit `prompts/`, `results.tsv`, and `LOOP_LOG.md` with the verdict
     line in the message. The candidate run becomes the baseline.
   - Exit 3 (REJECT): restore `prompts/` from git, and log the verdict line.
6. **Pace.** Schedule the next wake-up in 60 seconds while budget remains.

## Stop conditions

Stop the loop, and report to the owner, when any of these hold:

- Two consecutive accepted baselines each pass at least 90% of episodes. Confirm with one
  `--reps 3` run before declaring the prompts stable.
- Three consecutive rejected hypotheses target the same cluster. Its cause is probably not the
  prompt. Write down the premise the three shared and test it directly.
- A cluster needs an FCL change to fix.
- The spend budget is used up.

## Shipping the result

The tuned prompts belong in FCL, not in this harness:

- `shared.md` becomes the FCL MCP server's `instructions`.
- `tools.json` descriptions move into the FCL MCP tool definitions.
- `claude.md` and `openai.md` become the "paste this into your assistant" text in FCL's
  integration docs.

After an upstream move, empty the harness copy and re-baseline. The harness already adds the
server's instructions to every agent's system prompt.

## Fidelity limit

These runs drive the provider APIs, not the consumer apps. The Claude and ChatGPT apps add their
own system prompts and tool-confirmation dialogs, and they connect only to remote MCP servers
(HTTPS with OAuth), while FCL's MVP server is stdio-only. Before claiming that the apps
interoperate, FCL needs a remote MCP endpoint and a manual run of the dinner scenario in both apps.
