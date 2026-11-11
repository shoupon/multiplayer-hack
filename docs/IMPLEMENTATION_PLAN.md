# FCL MVP implementation plan

Companion to `docs/DESIGN.md`. Work lands as a sequence of units. Each unit ends in a
check that runs before the next unit starts.

## Definition of done

The MVP is done when all of these hold on a clean checkout:

1. `npm run check` (typecheck plus the full Vitest suite) passes.
2. `npm run demo` runs the dinner scenario (H§13) with two simulated agents, restarts the
   server mid-negotiation, and ends with the task in `AGREED`, a full transition history,
   and both participants reading the same outcome.
3. Every success criterion in the brief maps to a named, passing test (table below).
4. `docs/architecture.md`, `docs/protocol.md`, `docs/security.md`, `docs/integrations.md`,
   and `README.md` describe the code as built.

## Units

| # | Unit | Check |
|---|---|---|
| 0 | Design doc and this plan. | Reviewed by the owner. |
| 1 | Scaffold: `package.json`, `tsconfig`, Vitest, `.gitignore`. | `npm run typecheck` passes on an empty `src`. |
| 2 | Database layer: `Database` interface, PGlite and `pg` drivers, migration runner with an advisory lock, `0001_init.sql`. | Test: migrations apply to a fresh PGlite, a second run is a no-op, and the composite FKs reject a cross-family row. |
| 3 | Pure cores: prefixed ids, token generation and hashing, authorization policy, task state machine, envelope registry. | Unit tests per core, including every guard in DESIGN §10 and the grant precedence rules in §8. |
| 4 | Identity, families, invitations, agents, credentials, grants, audit writer, Fastify app and auth hook. | Integration tests over `app.inject`: signup, family creation, invitation accept, agent registration, discovery visibility, credential and grant revocation, removed member loses access. |
| 5 | Messaging: send, inbox poll with cursor, acknowledgement, read one message. | Tests: recipient consent required, cross-family send rejected, sender identity cannot be spoofed, idempotent resend returns the same message, offline recipient reads later by cursor. |
| 6 | Coordination tasks and approvals. | Tests: full dinner flow over HTTP, stale version rejected, expired proposal cannot be accepted, approval forgery rejected (other user, agent token), duplicate task create returns one task, negotiation cap, decline, cancel, complete. |
| 7 | Job queue and worker: expiry, follow-up reminders. | Tests: expiry job moves a task to `EXPIRED` once, a re-run is a no-op, reminders reach only participants who have not responded. |
| 8 | Restart durability. | Test: file-backed PGlite, create a task, close the app and database, reopen, respond, reach `AGREED`. |
| 9 | Webhooks: subscriptions, encrypted secrets, signing, SSRF guard, delivery job with backoff and dead letter. | Unit tests for the address classifier and signature. Integration test against a local receiver (insecure-loopback mode) that verifies the signature, then a failing receiver that ends in `dead` while the message stays in the inbox. |
| 10 | MCP stdio server over the REST API. | Test: in-process MCP client lists tools and drives create, inbox, and respond against a running FCL. |
| 11 | Demo: husband agent through MCP, wife companion through REST plus an approval step, server restart while the wife is offline. | `npm run demo` exits 0 and prints the history. |
| 12 | Documentation as built and README with setup and example requests. | Every request in the README runs against `npm start`. |
| 13 | Independent review of the branch (security and correctness), fixes for accepted findings. | Review findings resolved or answered. `npm run check` still passes. |

Units 2 and 3 have no dependency on each other. Every later unit depends on both.

## Success criteria to tests

| Brief success criterion (H§13) | Test |
|---|---|
| Two independently authenticated agents communicate through FCL. | `test/dinner-flow.test.ts` |
| Neither agent requires the same LLM provider. | `test/dinner-flow.test.ts` registers different `provider` labels; the core never reads them. |
| Both exchange structured messages. | `test/messaging.test.ts` |
| A counterproposal completes successfully. | `test/dinner-flow.test.ts` |
| The task persists across process restarts. | `test/restart.test.ts` |
| Unauthorized access is rejected. | `test/authz.test.ts`, `test/messaging.test.ts` |
| Duplicate messages do not create duplicate tasks. | `test/idempotency.test.ts` |
| Approval cannot be forged by another participant. | `test/approvals.test.ts` |
| Expired proposals cannot be accepted. | `test/expiry.test.ts` |
| An unavailable recipient can respond later. | `test/messaging.test.ts`, `test/restart.test.ts` |
| The complete task history is inspectable. | `test/dinner-flow.test.ts` asserts on `GET /v1/tasks/{id}` transitions. |

## Conventions

- Inputs are parsed by Zod at the HTTP boundary into branded domain types. Services trust
  their typed inputs.
- Each command is one database transaction.
- Tests use real authorization and a real Postgres engine (PGlite). No mocked auth.
- Time comes from an injected clock so expiry tests are deterministic.
- Commits are small and each one passes `npm run check`.
