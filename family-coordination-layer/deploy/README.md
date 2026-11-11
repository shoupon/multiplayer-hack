# Deploy FCL to Cloud Run

This runbook deploys FCL as its own Cloud Run service, `fcl`, in project
`fluent-observer-328812`, region `asia-east1`. It creates only resources whose names start with
`fcl`. It never edits an existing service, secret, service account, or bucket.

## What gets created

| Resource | Name |
|---|---|
| Artifact Registry repository | `asia-east1-docker.pkg.dev/fluent-observer-328812/fcl` |
| Runtime service account | `fcl-run@fluent-observer-328812.iam.gserviceaccount.com` |
| GCS bucket for Litestream | `gs://fcl-litestream-fluent-observer-328812` |
| Secrets | `fcl-server-key`, `fcl-signup-token` |
| Cloud Run service | `fcl` |

The service account gets two grants only: object admin on its own bucket and secret accessor on
its two secrets.

## Steps

`deploy/cloud-run.sh` prints every command. It runs them only when `APPLY=1` is set.

1. Review the commands:

	```sh
	bash deploy/cloud-run.sh setup
	bash deploy/cloud-run.sh deploy
	```

2. Create the resources once:

	```sh
	APPLY=1 bash deploy/cloud-run.sh setup
	```

3. Build, push, and deploy. Docker must be running:

	```sh
	APPLY=1 bash deploy/cloud-run.sh deploy
	```

Signup is open: the service does not mount `fcl-signup-token`, so `POST /v1/users` needs no
header. To gate signup, add `FCL_SIGNUP_TOKEN=fcl-signup-token:latest` to `--set-secrets` and
send the secret as the `X-Signup-Token` header.

## How it runs

- One instance, always on (`--min-instances=1 --max-instances=1`), with CPU always allocated
  (`--no-cpu-throttling`). SQLite allows one writer, and the job worker runs between requests.
- At start, `deploy/entrypoint.sh` restores the database from the bucket, then runs
  `python -m fcl serve` under `litestream replicate`. Litestream ships each commit to the bucket
  within about one second.
- Cloud Run terminates TLS. FCL does its own authentication, so the service allows
  unauthenticated invocations.
- `FCL_PUBLIC_URL` is the service's deterministic URL,
  `https://fcl-<project-number>.asia-east1.run.app`, so monitor links point there.

## Known risks

- **Rollout overlap.** During a deploy, the old and new revisions can run at the same time. Both
  replicate to the bucket, and writes to the old revision in that window can be lost. Deploy
  when no one is coordinating. The fix is a Cloud SQL PostgreSQL port.
- **Up to one second of loss.** A crash loses commits Litestream has not shipped yet.
- **Open signup.** Anyone who can reach the URL can create an account. A new account sees no
  family until a member invites it.
- **Gaps that remain for the MVP.** Tokens do not expire, the database is not encrypted at the
  application level, and rate-limit counts live in memory. The rate limiter is correct with one
  instance. See `docs/security.md`.
