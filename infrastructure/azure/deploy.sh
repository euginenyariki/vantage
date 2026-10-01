#!/usr/bin/env bash
#
# Deploy Vantage to Azure Container Apps.
#
# MODE=managed (default) provisions Azure Database for PostgreSQL and Azure Cache
# for Redis. Around $28/month for the pair, plus the Container Apps consumption
# bill, and it keeps billing whether or not anyone is using the platform. Data
# survives restarts and redeploys, which is what any deployment holding orders or
# positions requires.
#
# MODE=demo runs Postgres and Redis as internal container apps in the environment.
# Free, and nothing is charged for data storage. The cost is that the data does
# not survive: a consumption-only environment cannot hold a persistent volume, so
# every redeploy, image update, or scale-in wipes all users, bots, and backtests.
# Use it to try Vantage, never to run it.
#
# The two modes are not a migration path: they share a schema, so switching gives
# you an empty database rather than inheriting the other mode's data.
#
# Images are built with `az acr build`, which runs in ACR's own builders. That
# matters: this machine has no Docker daemon, so `az containerapp build` would
# not work.
#
# A Container App's FQDN is always `<app-name>.<environment-default-domain>`, and
# the default domain is known as soon as the platform is up. So the API, the
# dashboard, and the demo data stores all have predictable addresses before any of
# them exist. That is what lets the web image be built with the right values
# instead of waiting for a deployment to finish, and what lets the apps be given
# connection strings immediately. Predictions are verified afterwards.
#
# Secrets are generated on first run and reused afterwards. They are written to
# .azure-deploy-secrets with mode 600, which is gitignored. Rotating
# REFRESH_PEPPER would log everyone out; rotating CREDENTIAL_MASTER_KEY would
# make stored broker credentials undecryptable.

set -euo pipefail

REGION="${REGION:-eastus}"
PREFIX="${PREFIX:-vantage}"
IMAGE_TAG="${IMAGE_TAG:-v1}"
# 'managed' by default: order and position state has to survive a restart before
# this deployment is allowed anywhere near real money. Set MODE=demo to try it for
# free, accepting that the database is discarded on every redeploy.
MODE="${MODE:-managed}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SECRETS_FILE="$ROOT/.azure-deploy-secrets"

cd "$ROOT"

log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

case "$MODE" in
  demo|managed) ;;
  *) die "MODE must be 'demo' or 'managed', got '$MODE'" ;;
esac

command -v az >/dev/null || die "az CLI not found"
az account show >/dev/null 2>&1 || die "not signed in. Run: az login"

if [[ "$MODE" == demo ]]; then
  log "MODE=demo: Postgres and Redis run as containers in the environment. No data survives a redeploy."
fi

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
if [[ "$MODE" == managed ]]; then
  log "Platform: resource group, registry, Container Apps environment, Postgres, Redis"
else
  log "Platform: resource group, registry, Container Apps environment, Log Analytics"
fi

az deployment sub create \
  --location "$REGION" \
  --name vantage-platform \
  --template-file infrastructure/azure/main.bicep \
  --parameters \
      namePrefix="$PREFIX" \
      location="$REGION" \
      dataMode="$MODE" \
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

wait_for_replicas() {
  local app="$1" want="$2" label="$3" i ready
  for i in $(seq 1 40); do
    ready="$(az containerapp revision list -n "$app" -g "$RG" \
              --query "[?active].status.readyReplicas" -o tsv 2>/dev/null || echo 0)"
    [[ "${ready:-0}" -ge "$want" ]] && { log "$label ready after ~$((i * 15))s"; return 0; }
    [[ "$i" == 40 ]] && return 1
    sleep 15
  done
}

# In demo mode the data stores are container apps created by the platform phase
# above, and the API runs migrations on boot. Waiting here means the apps are
# deployed against a database that is already up, instead of crash-looping until
# Postgres finishes its first initdb.
if [[ "$MODE" == demo ]]; then
  log "Waiting for the demo Postgres and Redis to be ready"
  wait_for_replicas vantage-pg    1 "Postgres" || warn "Postgres not ready yet; the API will retry until it is."
  wait_for_replicas vantage-redis 1 "Redis"    || warn "Redis not ready yet; the API will retry."
fi

# ------------------------------------------------------------- managed-only check
# ARM's schema does not promise it honours an access key sent on create, and an
# unauthenticated cache on a public endpoint is a real exposure. Verify.
# In demo mode Redis is internal-only with no public IP, so there is nothing to
# expose and no key for ARM to drop.
if [[ "$MODE" == managed ]]; then
  log "Verifying the Redis access key was applied"
  # list-keys wants the resource name, not the hostname it resolves to.
  actual_key="$(az redis list-keys -n "redis-$PREFIX" -g "$RG" --query primaryKey -o tsv 2>/dev/null || true)"
  [[ "$actual_key" == "$REDIS_PASSWORD" ]] || die "Redis access key did not stick.
       Leaving it unset would expose an unauthenticated cache on a public IP.
       Set the key manually, or move the cache behind a private endpoint."
  log "Redis access key confirmed."
fi

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
      postgresUser="$([[ $MODE == managed ]] && echo vantageadmin || echo vantage)" \
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
web_rebuild_needed=0
for pair in "vantage-api:$API_ORIGIN" "vantage-web:$WEB_ORIGIN"; do
  app="${pair%%:*}"; predicted="${pair#*:}"
  actual="https://$(az containerapp show -n "$app" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)"
  if [[ "$actual" != "$predicted" ]]; then
    warn "$app is at $actual, not $predicted as predicted."
    # CORS and the rewrite target are both baked into the web image at build
    # time, so a wrong prediction means the web app has to be reconfigured, not
    # just rebuilt.
    web_rebuild_needed=1
  fi
done
if [[ "$web_rebuild_needed" == 1 ]]; then
  API_ORIGIN="https://$(az containerapp show -n vantage-api -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)"
  WEB_ORIGIN="https://$(az containerapp show -n vantage-web -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)"
  warn "Rebuilding the web image against the real origins, then updating the web app."
  build_image web infrastructure/docker/web.Dockerfile \
    --build-arg "API_INTERNAL_URL=$API_ORIGIN" \
    --build-arg "NEXT_PUBLIC_WS_URL=$API_ORIGIN"
  az containerapp update -n vantage-web -g "$RG" \
    --set env-vars API_INTERNAL_URL="$API_ORIGIN" NEXT_PUBLIC_WS_URL="$API_ORIGIN" >/dev/null || true
fi

# -------------------------------------------------------------------- readiness
log "Waiting for the API to report ready (the container runs migrations first)"
wait_for_replicas vantage-api 1 "API" \
  || warn "API not ready yet. Logs: az containerapp logs show -n vantage-api -g $RG --tail 100"

log "Health check"
curl -fsS --max-time 20 "$API_ORIGIN/api/health" && printf '\n' \
  || warn "health endpoint not answering; the revision may still be starting"

if [[ "$MODE" == demo ]]; then
  cat <<EOF

Deployed in demo mode.

  dashboard  $WEB_ORIGIN
  api        $API_ORIGIN
  api docs   $API_ORIGIN/docs

Everything in this deployment is disposable. Postgres and Redis are containers in
the Container Apps environment with nowhere to persist, so the next redeploy or
scale-in deletes every user, bot, and backtest. Nothing about it bills for data
storage. For a deployment that keeps its data:

  MODE=managed infrastructure/azure/deploy.sh
EOF
else
  cat <<EOF

Deployed.

  dashboard  $WEB_ORIGIN
  api        $API_ORIGIN
  api docs   $API_ORIGIN/docs

Postgres and Redis are billable and keep billing while the deployment exists.
To stop paying, delete the resource group:

  az group delete -n $RG --yes --no-wait
EOF
fi

cat <<'EOF'
Before this is usable:
  - PAPER mode. Live trading needs METAAPI_TOKEN plus a per-bot grant; see README.
  - There is no mailer, so email verification and password reset do not work.
    Create the first account through the dashboard's register form.
EOF