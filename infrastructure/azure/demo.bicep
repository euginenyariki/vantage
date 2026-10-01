// Ephemeral data tier, so Vantage can be tried without a ~$30/month bill.
//
// This is NOT a production data store, and it is not a thin wrapper around a
// good one. The constraints are physical:
//
//   - A consumption-only Container Apps environment cannot attach a persistent
//     volume, so there is nowhere for the data directory to live.
//   - The usual workaround, an Azure Files share mounted at /var/lib/postgresql,
//     corrupts the cluster. SMB does not give Postgres the byte-range locking it
//     requires, and the server refuses to come up cleanly.
//   - There is no room for backup or point-in-time restore either.
//
// So both stores lose everything on redeploy, image update, or scale-in. Treat
// the URL as disposable and demo-only. When the data matters, run deploy.sh with
// MODE=managed, which provisions the Postgres Flexible Server and Redis Cache
// defined in platform.bicep instead. Nothing above these two files changes.
//
// Both apps are pinned to a single replica. They sit idle by design, so there is
// nothing to autoscale against, and scaling to zero would guarantee the data loss
// rather than merely risk it.

@description('Container Apps environment name.')
param environmentName string

@description('Environment default domain, used to build the apps\' internal FQDNs.')
param environmentDefaultDomain string

@description('PostgreSQL login.')
param postgresUser string = 'vantage'

@description('PostgreSQL password.')
@secure()
param postgresPassword string

@description('Database name.')
param databaseName string = 'vantage'

@description('Redis password. Redis has no access-key semantics, so it is a requirepass value like any other.')
@secure()
param redisPassword string

@description('Data tier. \'managed\' omits these containers; \'demo\' creates them.')
@allowed([ 'managed', 'demo' ])
param dataMode string = 'demo'

var environmentId = resourceId('Microsoft.ContainerService/managedEnvironments', environmentName)

// -------------------------------------------------------------------- postgres
resource pg 'Microsoft.ContainerService/containerApps@2023-05-01' = if (dataMode == 'demo') {
  name: 'vantage-pg'
  location: resourceGroup().location
  properties: {
   environmentId: environmentId
   workloadProfileName: 'default'
   // Never external. Nothing outside the environment reaches the database, and
   // unlike the managed tier this needs no firewall exception to be reachable
   // from the apps.
   ingress: { external: false }
   template: {
    containers: [
     {
      name: 'postgres'
      image: 'postgres:17-alpine'
      resources: {
       requests: { cpu: '0.25', memory: '1Gi' }
      }
      env: [
       { name: 'POSTGRES_USER', value: postgresUser }
       { name: 'POSTGRES_PASSWORD', value: postgresPassword }
       { name: 'POSTGRES_DB', value: databaseName }
      ]
      // pg_isready does not authenticate; it only reports whether the server is
      // accepting connections. That is exactly the signal needed here, because
      // the API runs migrations on boot and fails loudly if Postgres is not up.
      probes: [
       {
        type: 'Readiness'
        exec: { command: [ 'pg_isready', '-U', postgresUser, '-d', databaseName ] }
        initialDelaySeconds: 8
        periodSeconds: 10
        failureThreshold: 6
       }
       {
        type: 'Liveness'
        exec: { command: [ 'pg_isready', '-U', postgresUser ] }
        periodSeconds: 20
        failureThreshold: 5
       }
      ]
     }
    ]
    scale: { minReplicas: 1, maxReplicas: 1 }
   }
  }
}

// ----------------------------------------------------------------------- redis
// Redis carries the event bus and the backtest queue. Losing it is survivable in
// a way losing Postgres is not: the workers reconcile their jobs from Postgres on
// startup, so the bus can be rebuilt and re-populated.
resource redis 'Microsoft.ContainerService/containerApps@2023-05-01' = if (dataMode == 'demo') {
  name: 'vantage-redis'
  location: resourceGroup().location
  properties: {
   environmentId: environmentId
   workloadProfileName: 'default'
   ingress: { external: false }
   template: {
    containers: [
     {
      name: 'redis'
      image: 'redis:7-alpine'
      // Password is required even on a private network. An unauthenticated Redis
      // is a standing footgun, and setting it costs nothing.
      //
      // `sh -c` is doing real work here: it expands $REDISCLI_AUTH from the
      // container environment at start-up, which keeps the value out of the
      // container app spec entirely. Hardcoding it into the command array would
      // expose it to anyone who can read the resource.
      command: [
       'sh'
       '-c'
       'exec redis-server --requirepass "$REDISCLI_AUTH" --save "" --appendonly no'
      ]
      resources: {
       requests: { cpu: '0.25', memory: '256Mi' }
      }
      env: [
       {
        // redis-cli reads this variable for its own authentication, so the
        // readiness probe needs no password on its command line.
        name: 'REDISCLI_AUTH'
        value: redisPassword
       }
      ]
      probes: [
       {
        type: 'Readiness'
        exec: { command: [ 'redis-cli', 'ping' ] }
        initialDelaySeconds: 5
        periodSeconds: 10
        failureThreshold: 6
       }
      ]
     }
    ]
    scale: { minReplicas: 1, maxReplicas: 1 }
   }
  }
}

// Predictable FQDNs, so deploy.sh can pass connection strings to the apps before
// these two apps have finished starting.
output pgHost string = 'vantage-pg.${environmentName}.${environmentDefaultDomain}'
output redisHost string = 'vantage-redis.${environmentName}.${environmentDefaultDomain}'