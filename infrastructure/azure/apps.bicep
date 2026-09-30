// Vantage container apps. Deployed after platform.bicep, and in two phases.
//
// Why two phases: the web image bakes the API's FQDN into its Next.js rewrites
// and its client bundle at build time, and that FQDN is only known once the API
// app exists. deploy.sh therefore provisions api/engine/worker first, reads the
// API FQDN back, builds the web image with it, and only then calls
// `az containerapp update` on the web app.
//
// Every app gets a system-assigned identity with AcrPull on the registry, so no
// image is pulled with a shared admin password. Secrets are Container Apps
// secrets, which are encrypted at rest but readable by anyone with
// contributor rights on the environment; a Key Vault reference is the stricter
// choice and is not wired up yet.

@description('Container Apps environment name.')
param environmentName string

@description('Container Registry login server, e.g. acrfoo.azurecr.io.')
param acrLoginServer string

@description('ACR name. Referenced as an existing resource so the AcrPull scope is a real resource reference, not a string.')
param acrName string

@description('Image tag to run.')
param imageTag string = 'v1'

@description('PostgreSQL fully qualified domain name.')
param postgresFqdn string

@description('Database name.')
param databaseName string = 'vantage'

@description('PostgreSQL administrator login.')
param postgresUser string

@description('PostgreSQL password.')
@secure()
param postgresPassword string

@description('Redis host.')
param redisHost string

@description('Redis port.')
param redisPort string = '6379'

@description('Redis password, URL-encoded form.')
@secure()
param redisPasswordEncoded string

@description('Public origin of the dashboard, for CORS.')
param webOrigin string

@description('32-byte base64 key that encrypts stored broker credentials.')
@secure()
param credentialMasterKey string

@description('Access token signing key.')
@secure()
param jwtSecret string

@description('Refresh token pepper.')
@secure()
param refreshPepper string

@description('Image tag for the web app, built after the API exists.')
param webImageTag string = imageTag

var common = {
 ENVIRONMENT: 'production'
 TRADING_MODE: 'PAPER'
  // Live execution stays off. Flipping this is not sufficient on its own: the
  // engine also requires a per-bot grant and a non-demo broker account.
 LIVE_TRADING_ENABLED: 'false'
 TZ: 'UTC'
 DATABASE_URL: 'postgresql+asyncpg://${postgresUser}:${postgresPassword}@${postgresFqdn}:5432/${databaseName}'
 REDIS_URL: 'redis://:${redisPasswordEncoded}@${redisHost}:${redisPort}/0'
 CREDENTIAL_MASTER_KEY: credentialMasterKey
 CREDENTIAL_KEY_VERSION: '1'
 JWT_SECRET: jwtSecret
 REFRESH_PEPPER: refreshPepper
 JWT_ALGORITHM: 'HS256'
 ACCESS_TOKEN_MINUTES: '30'
 REFRESH_TOKEN_DAYS: '30'
  // The dashboard is the only browser origin. Same-site subdomains of
  // azurecontainerapps.io still send the refresh cookie.
 COOKIE_SECURE: 'true'
 CORS_ORIGINS: webOrigin
  // No broker credentials are shipped. Paper trading needs none, and the
  // MetaApi endpoints return 503 until a token is added.
 METAAPI_ENABLED: 'false'
 METAAPI_TOKEN: ''
 METAAPI_API_URL: 'https://sandbox.app.metaapi.cloud'
}

resource api 'Microsoft.ContainerService/containerApps@2023-05-01' = {
  name: 'vantage-api'
  location: resourceGroup().location
  identity: { type: 'SystemAssigned' }
  properties: {
   environmentId: environmentId
   workloadProfileName: 'default'
   ingress: {
    external: true
    targetPort: 8000
    transport: 'auto'
    // The socket needs the browser to reach this app directly, and the REST
    // rewrite in the web app needs a public target too.
    allowInsecure: false
    traffic: [
     { latestRevision: true, weight: 100 }
    ]
   }
   template: {
    containers: [
     {
      name: 'api'
      image: '${acrLoginServer}/vantage-api:${imageTag}'
      resources: {
       requests: { cpu: '0.5', memory: '1Gi' }
      }
      env: common
      probes: [
       {
        type: 'Liveness'
        httpGet: { path: '/api/health', port: 8000 }
        initialDelaySeconds: 20
        periodSeconds: 20
        failureThreshold: 5
       }
       {
        type: 'Readiness'
        httpGet: { path: '/api/health', port: 8000 }
        initialDelaySeconds: 10
        periodSeconds: 10
        failureThreshold: 6
       }
      ]
     }
    ]
    // Zero replicas would leave the migrations unapplied, because nothing would
    // start the container that runs them.
    scale: {
     minReplicas: 1
     maxReplicas: 3
     rules: [
      {
       type: 'http'
       http: { metadata: { concurrentRequests: '20' } }
      }
     ]
    }
   }
  }
}

// The engine and the backtest worker are not HTTP services, so HTTP autoscaling
// cannot wake them. They hold a minimum of one replica or a queued backtest
// would sit unprocessed until someone scaled them by hand.
resource engine 'Microsoft.ContainerService/containerApps@2023-05-01' = {
  name: 'vantage-engine'
  location: resourceGroup().location
  identity: { type: 'SystemAssigned' }
  properties: {
   environmentId: environmentId
   workloadProfileName: 'default'
   ingress: { external: false }
   template: {
    containers: [
     {
      name: 'engine'
      image: '${acrLoginServer}/vantage-engine:${imageTag}'
      resources: {
       requests: { cpu: '0.5', memory: '1Gi' }
      }
      env: union(common, {
       ENGINE_TICK_SECONDS: '1'
       RECONCILE_INTERVAL_SECONDS: '30'
       MARKET_DATA_STALE_SECONDS: '15'
      })
     }
    ]
    scale: {
     minReplicas: 1
     maxReplicas: 2
     rules: [
      {
       type: 'cpu'
       cpu: { metadata: { value: '70' } }
      }
     ]
    }
   }
  }
}

resource worker 'Microsoft.ContainerService/containerApps@2023-05-01' = {
  name: 'vantage-backtest-worker'
  location: resourceGroup().location
  identity: { type: 'SystemAssigned' }
  properties: {
   environmentId: environmentId
   workloadProfileName: 'default'
   ingress: { external: false }
   template: {
    containers: [
     {
      name: 'backtest-worker'
      // Same code and dependencies as the engine, so it shares the image rather
      // than paying for an identical second build. Only the entrypoint differs.
      image: '${acrLoginServer}/vantage-engine:${imageTag}'
      command: [ 'python', '-m', 'trading_engine.backtest_worker' ]
      resources: {
       requests: { cpu: '0.5', memory: '1Gi' }
      }
      env: common
     }
    ]
    scale: {
     minReplicas: 1
     maxReplicas: 3
     rules: [
      {
       type: 'cpu'
       cpu: { metadata: { value: '70' } }
      }
     ]
    }
   }
  }
}

// Created last and updated in place by deploy.sh, once the web image has been
// built against the API's real FQDN.
resource web 'Microsoft.ContainerService/containerApps@2023-05-01' = {
  name: 'vantage-web'
  location: resourceGroup().location
  identity: { type: 'SystemAssigned' }
  properties: {
   environmentId: environmentId
   workloadProfileName: 'default'
   ingress: {
    external: true
    targetPort: 3000
    transport: 'auto'
    allowInsecure: false
    traffic: [
     { latestRevision: true, weight: 100 }
    ]
   }
   template: {
    containers: [
     {
      name: 'web'
      image: '${acrLoginServer}/vantage-web:${webImageTag}'
      resources: {
       requests: { cpu: '0.25', memory: '512Mi' }
      }
      probes: [
       {
        type: 'Liveness'
        httpGet: { path: '/', port: 3000 }
        periodSeconds: 20
        failureThreshold: 5
       }
      ]
     }
    ]
    scale: {
     minReplicas: 1
     maxReplicas: 3
     rules: [
      {
       type: 'http'
       http: { metadata: { concurrentRequests: '30' } }
      }
     ]
    }
   }
  }
}

var environmentId = resourceId('Microsoft.ContainerService/managedEnvironments', environmentName)
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource acr 'Microsoft.ContainerRegistry/registries@2021-09-01' existing = {
  name: acrName
}

// AcrPull for each app's system-assigned identity, so no image is ever pulled
// with a shared admin password. Written out one per app rather than in a loop:
// a for-expression has to be resolvable at the start of the deployment, and a
// freshly created identity's principalId is not.
resource pullApi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(api.id, acr.id, acrPullRoleId)
  scope: acr
  properties: {
   principalId: api.identity.principalId
   roleDefinitionId: resourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
   principalType: 'ServicePrincipal'
  }
}

resource pullEngine 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(engine.id, acr.id, acrPullRoleId)
  scope: acr
  properties: {
   principalId: engine.identity.principalId
   roleDefinitionId: resourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
   principalType: 'ServicePrincipal'
  }
}

resource pullWorker 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(worker.id, acr.id, acrPullRoleId)
  scope: acr
  properties: {
   principalId: worker.identity.principalId
   roleDefinitionId: resourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
   principalType: 'ServicePrincipal'
  }
}

resource pullWeb 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(web.id, acr.id, acrPullRoleId)
  scope: acr
  properties: {
   principalId: web.identity.principalId
   roleDefinitionId: resourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
   principalType: 'ServicePrincipal'
  }
}

output apiUrl string = 'https://${api.properties.configuration.ingress.fqdn}'
output webUrl string = 'https://${web.properties.configuration.ingress.fqdn}'
