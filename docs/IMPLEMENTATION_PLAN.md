# FCL MVP implementation plan

Companion to `docs/DESIGN.md`. Work lands as a sequence of units. Each unit ends in a
check that runs before the next unit starts.

Everything runs on one laptop with Python 3.10+: a local virtualenv, one SQLite file, no
Docker, no database server, no external service.

## Definition of done

The MVP is done when all of these hold on a clean checkout:

1. `make check` (pytest, the full suite) passes. Setup is `make install`, which creates
   `.venv` and installs the package with its dev dependencies.
2. `make demo` runs the dinner scenario (H§13) with two simulated agents as separate
   processes, restarts the FCL server process mid-negotiation, and ends with the task in
   `AGREED`, a full transition history, and both participants reading the same outcome.
3. Every success criterion in the brief maps to a named, passing test (table below).
4. `docs/architecture.md`, `docs/protocol.md`, `docs/security.md`, `docs/integrations.md`,
   and `README.md` describe the code as built.

## Units

| # | Unit | Check |
|---|---|---|
| 0 | Design doc and this plan. | Reviewed by the owner. |
| 1 | Scaffold: `pyproject.toml`, `Makefile`, `.gitignore`, package skeleton. | `make install` succeeds and `python -m fcl --help` runs. |
| 2 | Database layer: connection factory (WAL, foreign keys, busy timeout), `BEGIN IMMEDIATE` transaction helper, migration runner, `0001_init.sql`. | Tests: migrations apply to a fresh file, a second run is a no-op, composite FKs reject a cross-family row and a grant from a non-owner. |
| 3 | Pure cores: ids, token generation and hashing, authorization policy, task state machine, envelope registry. | Unit tests per core, including every guard in DESIGN section 10 and the grant precedence rules in section 8. |
| 4 | Identity, families, invitations, agents, credentials, grants, audit writer, FastAPI app and auth dependency. | Integration tests over `TestClient`: signup, family creation, invitation accept, agent registration, discovery visibility, credential and grant revocation, removed member loses access. |
| 5 | Messaging: send, inbox poll with cursor, acknowledgement, read one message. | Tests: recipient consent required, cross-family send rejected, sender identity cannot be spoofed, idempotent resend returns the same message, offline recipient reads later by cursor. |
| 6 | Coordination tasks and approvals. | Tests: full dinner flow over HTTP, stale version rejected, expired proposal cannot be accepted, approval forgery rejected (other user, agent token), duplicate task create returns one task, negotiation cap, decline, cancel, complete. |
| 7 | Job queue and worker thread: expiry, follow-up reminders. | Tests: expiry job moves a task to `EXPIRED` once, a re-run is a no-op, reminders reach only participants who have not responded. |
| 8 | Restart durability. | Test: create a task, discard the app, build a new app on the same SQLite file, respond, reach `AGREED`. |
| 9 | Webhooks: subscriptions, encrypted secrets, signing, SSRF guard, delivery job with backoff and dead letter. | Unit tests for the address classifier and signature. Integration test against a local receiver (loopback mode) that verifies the signature, then a failing receiver that ends in `dead` while the message stays in the inbox. |
| 10 | MCP stdio server over the REST API. | Test: an MCP client session over stdio lists the tools and drives create, inbox, and respond against a live local FCL server. |
| 11 | Demo: husband agent through MCP, wife companion through REST plus an approval step, FCL server process restarted while the wife is offline. | `make demo` exits 0 and prints the history. |
| 12 | Documentation as built, and a README with setup and example requests. | Every request in the README runs against `make serve`. |
| 13 | Independent review of the branch (security and correctness), fixes for accepted findings. | Findings resolved or answered. `make check` still passes. |

Units 2 and 3 have no dependency on each other. Every later unit depends on both.

## Success criteria to tests

| Brief success criterion (H§13) | Test |
|---|---|
| Two independently authenticated agents communicate through FCL. | `tests/test_dinner_flow.py` |
| Neither agent requires the same LLM provider. | `tests/test_dinner_flow.py` registers different `provider` labels; the core never reads them. |
| Both exchange structured messages. | `tests/test_messaging.py` |
| A counterproposal completes successfully. | `tests/test_dinner_flow.py` |
| The task persists across process restarts. | `tests/test_restart.py`, plus the real process restart in `make demo` |
| Unauthorized access is rejected. | `tests/test_identity_agents.py`, `tests/test_messaging.py` |
| Duplicate messages do not create duplicate tasks. | `tests/test_tasks.py` |
| Approval cannot be forged by another participant. | `tests/test_tasks.py` |
| Expired proposals cannot be accepted. | `tests/test_tasks.py`, `tests/test_jobs.py` |
| An unavailable recipient can respond later. | `tests/test_messaging.py`, `tests/test_restart.py` |
| The complete task history is inspectable. | `tests/test_dinner_flow.py` asserts on `GET /v1/tasks/{id}` transitions. |

## Conventions

- Inputs are parsed by Pydantic at the HTTP boundary. Services trust their typed inputs.
- Each command is one `BEGIN IMMEDIATE` transaction.
- Tests use real authorization and a real SQLite file in a temporary directory. No mocked
  auth.
- Time comes from an injected clock so expiry tests are deterministic.
- Commits are small and each one passes `make check`.
