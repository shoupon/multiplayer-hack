#!/usr/bin/env bash
# Deploys FCL as its own Cloud Run service. Prints every command; runs them only with APPLY=1.
#
#   bash deploy/cloud-run.sh setup    # once: registry, service account, bucket, secrets, IAM
#   bash deploy/cloud-run.sh deploy   # build, push, deploy
#
# Touches only resources named fcl*. It never edits an existing service, secret, account, or bucket.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-fluent-observer-328812}"
REGION="${REGION:-asia-east1}"
SERVICE="${SERVICE:-fcl}"
SA="fcl-run@${PROJECT_ID}.iam.gserviceaccount.com"
BUCKET="${BUCKET:-fcl-litestream-${PROJECT_ID}}"
REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/fcl"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

run() {
	printf '+ %s\n' "$*"
	if [ "${APPLY:-0}" = 1 ]; then "$@"; fi
}

setup() {
	run gcloud artifacts repositories create fcl --project="$PROJECT_ID" --location="$REGION" \
		--repository-format=docker --description="Family Coordination Layer images"
	run gcloud iam service-accounts create fcl-run --project="$PROJECT_ID" --display-name="FCL Cloud Run runtime"
	run gcloud storage buckets create "gs://${BUCKET}" --project="$PROJECT_ID" --location="$REGION" \
		--uniform-bucket-level-access --public-access-prevention
	run gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
		--member="serviceAccount:${SA}" --role=roles/storage.objectAdmin
	printf '+ head -c 32 /dev/urandom | base64 | gcloud secrets create fcl-server-key --data-file=- ...\n'
	if [ "${APPLY:-0}" = 1 ]; then
		head -c 32 /dev/urandom | base64 | gcloud secrets create fcl-server-key --project="$PROJECT_ID" \
			--replication-policy=user-managed --locations="$REGION" --data-file=-
	fi
	printf '+ openssl rand -hex 24 | gcloud secrets create fcl-signup-token --data-file=- ...\n'
	if [ "${APPLY:-0}" = 1 ]; then
		openssl rand -hex 24 | tr -d '\n' | gcloud secrets create fcl-signup-token --project="$PROJECT_ID" \
			--replication-policy=user-managed --locations="$REGION" --data-file=-
	fi
	for secret in fcl-server-key fcl-signup-token; do
		run gcloud secrets add-iam-policy-binding "$secret" --project="$PROJECT_ID" \
			--member="serviceAccount:${SA}" --role=roles/secretmanager.secretAccessor
	done
}

deploy() {
	local tag image number
	tag="$(git -C "$ROOT" rev-parse --short HEAD)"
	if [ -n "$(git -C "$ROOT" status --porcelain -- .)" ]; then tag="${tag}-dirty-$(date +%Y%m%d%H%M%S)"; fi
	image="${REPO}/fcl:${tag}"
	number="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
	run gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
	run docker buildx build --platform linux/amd64 -t "$image" --push "$ROOT"
	# One instance, always on, CPU always allocated: SQLite has one writer and the worker runs between requests.
	run gcloud run deploy "$SERVICE" --project="$PROJECT_ID" --region="$REGION" --image="$image" \
		--service-account="$SA" --allow-unauthenticated \
		--min-instances=1 --max-instances=1 --no-cpu-throttling --cpu=1 --memory=1Gi --concurrency=80 \
		--set-env-vars="FCL_LITESTREAM_BUCKET=${BUCKET},FCL_PUBLIC_URL=https://${SERVICE}-${number}.${REGION}.run.app,FCL_TRUST_FORWARDED_FOR=true" \
		--set-secrets="FCL_SERVER_KEY=fcl-server-key:latest"
}

case "${1:-}" in
	setup) setup ;;
	deploy) deploy ;;
	*) echo "usage: [APPLY=1] $0 setup|deploy" >&2; exit 2 ;;
esac
