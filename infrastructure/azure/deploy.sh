#!/usr/bin/env bash
#
# Deploy Vantage to Azure Container Apps.
#
# Images are built with `az acr build`, which runs in ACR's own builders. That
# matters: this machine has no Docker daemon, so `az containerapp build` would
# not work.
#
# A Container App's FQDN is always `<app-name>.<environment-default-domain>`, and
# the default domain is known as soon as the platform is up. So the API and
# dashboard origins can be predicted before either app exists, which is what
# lets the web image be built with the right values instead of waiting for a
# deployment to finish. The prediction is verified afterwards, and a mismatch
# triggers a rebuild rather than a silently misconfigured client bundle.
#
# Secrets are generated on first run and reused afterwards. They are written to
# .azure-deploy-secrets with mode 600, which is gitignored. Rotating
# REFRESH_PEPPER would log everyone out; rotating CREDENTIAL_MASTER_KEY would
# make stored broker credentials undecryptable.

set -euo pipefail

REGION="${REGION:-eastus}"
PREFIX="${PREFIX:-vantage}"
IMAGE_TAG="${IMAGE_TAG:-v1}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SECRETS_FILE="$ROOT/.azure-deploy-secrets"

cd "$ROOT"

log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

command -v az >/dev/null || die "az CLI not found"
az account show >/dev/null 2>&1 || die "not signed in. Run: az login"

# --------------------------------------------------------------------- secrets
if [[ -f "$SECRETS_FILE" ]]; then
  log "Reusing $SECRETS_FILE"
  # shellcheck disable=SC1090
  source "$SECRETS_FILE"
else
  log "Generating deployment secrets -> $SECRETS_FILE (mode 600)"
  gen() { python3 -c "import secrets; print(secrets.token_urlsafe(48))"; }
  {
    echo "PG_PASSWORD=$(gen)"
    echo "REDIS_PASSWORD=$(gen)"
    echo "JWT_SECRET=$(gen)"
    echo "REFRESH_PEPPER=$(gen)"
    echo "CREDENTIAL_MASTER_KEY=$(python3 -c 'import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())')"
  } > "$SECRETS_FILE"
  chmod 600 "$SECRETS_FILE"
  # shellcheck disable=SC1090
  source "$SECRETS_FILE"
  warn "New secrets created. Back up $SECRETS_FILE; losing it loses stored data."
fi

redis_encoded="$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$REDIS_PASSWORD")"

# --------------------------------------------------------------------- platform
log "Platform: resource group, registry, Container Apps environment, Postgres, Redis"
az deployment sub create \
  --location "$REGION" \
  --name vantage-platform \
  --template-file infrastructure/azure/main.bicep \
  --parameters \
      namePrefix="$PREFIX" \
      location="$REGION" \
      postgresAdminUser=vantageadmin \
      postgresAdminPassword="$PG_PASSWORD" \
      redisPassword="$REDIS_PASSWORD" >/dev/null

out() { az deployment sub show --name vantage-platform --query "properties.outputs.$1.value" -o tsv; }

RG="rg-$PREFIX"
ACR="$(out acrName)"
ENV_NAME="$(out environmentName)"
DEFAULT_DOMAIN="$(out environmentDefaultDomain)"
PG_FQDN="$(out postgresFqdn)"
REDIS_HOST="$(out redisHost)"

# ARM's schema does not promise it honours an access key sent on create, and an
# unauthenticated cache on a public endpoint is a real exposure. Verify.
log "Verifying the Redis access key was applied"
actual_key="$(az redis list-keys -n "$REDIS_HOST" -g "$RG" --query primaryKey -o tsv 2>/dev/null || true)"
[[ "$actual_key" == "$REDIS_PASSWORD" ]] || die "Redis access key did not stick.
       Leaving it unset would expose an unauthenticated cache on a public IP.
       Set the key manually, or move the cache behind a private endpoint."
log "Redis access key confirmed."

# Predicted origins, verified against reality after deployment.
API_ORIGIN="https://vantage-api.$DEFAULT_DOMAIN"
WEB_ORIGIN="https://vantage-web.$DEFAULT_DOMAIN"

# ----------------------------------------------------------------------- images
build_image() {
  local name="$1" dockerfile="$2"; shift 2
  log "Building vantage-$name:$IMAGE_TAG in ACR"
  az acr build --registry "$ACR" --image "vantage-$name:$IMAGE_TAG" \
    --file "$dockerfile" "$@" . >/dev/null
}

build_image api    infrastructure/docker/api.Dockerfile
build_image engine infrastructure/docker/engine.Dockerfile
# The backtest worker shares the engine image and overrides its entrypoint in
# apps.bicep, so only three images are built.
build_image web    infrastructure/docker/web.Dockerfile \
  --build-arg "API_INTERNAL_URL=$API_ORIGIN" \
  --build-arg "NEXT_PUBLIC_WS_URL=$API_ORIGIN"

# ------------------------------------------------------------------ containers
log "Container Apps: api, engine, backtest worker, web"
az deployment group create \
  --resource-group "$RG" \
  --name vantage-apps \
  --template-file infrastructure/azure/apps.bicep \
  --parameters \
      environmentName="$ENV_NAME" \
      acrName="$ACR" \
      acrLoginServer="$ACR.azurecr.io" \
      imageTag="$IMAGE_TAG" \
      webImageTag="$IMAGE_TAG" \
      postgresFqdn="$PG_FQDN" \
      databaseName="$PREFIX" \
      postgresUser=vantageadmin \
      postgresPassword="$PG_PASSWORD" \
      redisHost="$REDIS_HOST" \
      redisPort=6379 \
      redisPasswordEncoded="$redis_encoded" \
      webOrigin="$WEB_ORIGIN" \
      credentialMasterKey="$CREDENTIAL_MASTER_KEY" \
      jwtSecret="$JWT_SECRET" \
      refreshPepper="$REFRESH_PEPPER" >/dev/null

# -------------------------------------------------------------- verify FQDNs
# The web bundle was built against predicted origins. If the prediction was
# wrong the client would silently point at the wrong host, so check and rebuild.
log "Confirming the predicted origins matched"
for pair in "vantage-api:$API_ORIGIN" "vantage-web:$WEB_ORIGIN"; do
  app="${pair%%:*}"; predicted="${pair#*:}"
  actual="https://$(az containerapp show -n "$app" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)"
  if [[ "$actual" != "$predicted" ]]; then
    warn "$app is at $actual but the web image was built for $predicted. Rebuilding."
    build_image web infrastructure/docker/web.Dockerfile \
      --build-arg "API_INTERNAL_URL=$API_ORIGIN" \
      --build-arg "NEXT_PUBLIC_WS_URL=$API_ORIGIN"
    break
  fi
done

# -------------------------------------------------------------------- readiness
log "Waiting for the API to report ready (the container runs migrations first)"
for i in $(seq 1 40); do
  ready="$(az containerapp revision list -n vantage-api -g "$RG" \
            --query "[?active].status.readyReplicas" -o tsv 2>/dev/null || echo 0)"
  [[ "${ready:-0}" -ge 1 ]] && { log "API ready after ~$((i * 15))s"; break; }
  [[ "$i" == 40 ]] && warn "API not ready yet. Logs: az containerapp logs show -n vantage-api -g $RG --tail 100"
  sleep 15
done

log "Health check"
curl -fsS --max-time 20 "$API_ORIGIN/api/health" && printf '\n' \
  || warn "health endpoint not answering; the revision may still be starting"

cat <<EOF

Deployed.

  dashboard  $WEB_ORIGIN
  api        $API_ORIGIN
  api docs   $API_ORIGIN/docs

Before this is usable:
  - PAPER mode. Live trading needs METAAPI_TOKEN plus a per-bot grant; see README.
  - There is no mailer, so email verification and password reset do not work.
    Create the first account through the dashboard's register form.
  - Postgres and Redis sit on public endpoints behind an "allow Azure services"
    firewall. Acceptable for a demo, not for real money.
EOF
