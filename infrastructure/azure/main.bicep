// Entry point. Creates the resource group, then delegates the platform to a
// resource-group-scoped module, because a subscription-scope file cannot declare
// resource-group-scoped resources inline.
//
// The container apps live in apps.bicep and are deployed as a separate phase:
// the web image bakes the API's internal FQDN into its Next.js rewrites at build
// time, so the API has to exist before the web image is built. See deploy.sh.

targetScope = 'subscription'

@description('Deployment region.')
param location string

@description('Short name prefix, used for globally unique names.')
@minLength(3)
@maxLength(20)
param namePrefix string = 'vantage'

@description('PostgreSQL major version. 17 matches local development; 16 is the fallback if the region rejects it.')
@allowed(['16', '17'])
param postgresVersion string = '17'

@description('PostgreSQL administrator login.')
param postgresAdminUser string = 'vantageadmin'

@description('PostgreSQL administrator password. Supply from a secret store; never commit it.')
@secure()
param postgresAdminPassword string

@description('Redis access key. Supply from a secret store; never commit it.')
@secure()
param redisPassword string

@description('Data tier. \'managed\' provisions billable Postgres and Redis, about $28/month. \'demo\' runs both as internal container apps in the environment: free, but every redeploy or scale-in wipes all data.')
@allowed([ 'managed', 'demo' ])
param dataMode string = 'managed'

module platform 'platform.bicep' = {
  name: 'vantage-platform'
  scope: resourceGroup(resourceGroupName)
  params: {
   location: location
   dataMode: dataMode
   namePrefix: namePrefix
   postgresVersion: postgresVersion
   postgresAdminUser: postgresAdminUser
   postgresAdminPassword: postgresAdminPassword
   redisPassword: redisPassword
  }
}

// Ephemeral data tier.
//
// Deployed unconditionally, with the resource-level condition inside demo.bicep.
// That is deliberate: a conditional *module* has no outputs at deployment time, so
// the output expressions below would fail to resolve in managed mode. Guarding the
// resources instead keeps every output in this file valid in both modes, and the
// FQDNs are computed from parameters rather than read back from ARM, so they are
// valid before the apps have started.
//
// The implicit dependency on the platform module keeps this after the environment
// exists, which is the only prerequisite.
module demo 'demo.bicep' = {
  name: 'vantage-demo-data'
  scope: resourceGroup(resourceGroupName)
  params: {
   dataMode: dataMode
   environmentName: platform.outputs.environmentName
   environmentDefaultDomain: platform.outputs.environmentDefaultDomain
   postgresUser: 'vantage'
   postgresPassword: postgresAdminPassword
   redisPassword: redisPassword
   databaseName: namePrefix
  }
}

var resourceGroupName = 'rg-${namePrefix}'

resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: resourceGroupName
  location: location
  tags: {
 project: namePrefix
 managedBy: 'bicep'
 }
}

output resourceGroupName string = rg.name
output environmentName string = platform.outputs.environmentName
output environmentDefaultDomain string = platform.outputs.environmentDefaultDomain
output acrName string = platform.outputs.acrName
output acrLoginServer string = platform.outputs.acrLoginServer
output dataMode string = dataMode
// The managed tier exposes the services directly; the demo tier fronts containers
// on predictable internal FQDNs. Both carry the same port, so one connection
// string shape covers either.
output postgresFqdn string = dataMode == 'managed' ? platform.outputs.postgresFqdn : demo.outputs.pgHost
output redisHost string = dataMode == 'managed' ? platform.outputs.redisHost : demo.outputs.redisHost
output redisPort int = platform.outputs.redisPort
output workspaceName string = platform.outputs.workspaceName
