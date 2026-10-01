// Platform resources for Vantage. Deployed by main.bicep into the resource
// group; see that file for the subscription-scope wrapper.
//
// Data stores are reachable over public IPs so the Container Apps can connect
// without a VNet. Postgres is password-authenticated, Redis uses an access key.
// That is a demo-grade network posture: the honest upgrade is a private endpoint
// plus environment VNet integration. See docs/known-gaps.md.
//
// With dataMode 'demo' the managed stores below are not created at all, and
// demo.bicep runs Postgres and Redis as internal container apps instead. The
// registry, log workspace, and environment are identical in both modes, so they
// live here unconditionally and cost nothing on their own.

@description('Deployment region.')
param location string

@description('Data tier: \'managed\' for the Postgres and Redis below, \'demo\' for ephemeral containers in the environment.')
@allowed([ 'managed', 'demo' ])
param dataMode string = 'managed'

@description('Short name prefix, used for globally unique names.')
param namePrefix string

@description('PostgreSQL major version.')
param postgresVersion string

@description('PostgreSQL administrator login.')
param postgresAdminUser string

@description('PostgreSQL administrator password.')
@secure()
param postgresAdminPassword string

@description('Redis access key.')
@secure()
param redisPassword string

var suffix = uniqueString(subscription().id)
var envName = 'cae-${namePrefix}'
var acrName = 'acr${namePrefix}${suffix}'
var logName = 'log-${namePrefix}'
var pgName = 'pg-${namePrefix}-${suffix}'
var redisName = 'redis-${namePrefix}'

// ---------------------------------------------------------------- observability
// Container Apps requires a workspace for system logs.
resource log 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: logName
  location: location
  properties: {
 sku: { name: 'PerGB2018' }
 retentionInDays: 30
 }
}

// --------------------------------------------------------------------- container
resource acr 'Microsoft.ContainerRegistry/registries@2021-09-01' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: {
 adminUserEnabled: false // the apps pull with a managed identity
 networkRuleSet: {
  defaultAction: 'Deny'
  bypass: 'AzureServices'
 }
 }
}

// ACR Tasks push from outside the VNet, so the registry's own service tag has to
// be allowed explicitly or the first `az acr build` is rejected.
resource acrTaskRule 'Microsoft.ContainerRegistry/registries/networkRules@2021-09-01' = {
  parent: acr
  name: 'allow-acr-tasks'
  properties: {
  action: 'Allow'
  scope: 'Registry'
  serviceTags: [ 'AzureContainerRegistry' ]
 }
}

resource env 'Microsoft.ContainerService/managedEnvironments@2023-05-01' = {
  name: envName
  location: location
  properties: {
 appLogsConfiguration: {
  destination: 'log-analytics'
  logAnalyticsConfiguration: {
   customerId: log.properties.customerId
   sharedKey: log.listKeys().primarySharedKey
  }
 }
 zoneRedundant: false
 }
}

// ----------------------------------------------------------------------- data
// Managed data tier, billable at roughly $28/month for the pair. Skipped
// entirely in demo mode; see the file header for what replaces it.
resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2022-09-01' = if (dataMode == 'managed') {
  name: pgName
  location: location
  properties: {
 version: postgresVersion
 administratorLogin: postgresAdminUser
 administratorLoginPassword: postgresAdminPassword
 storage: {
  storageSizeGB: 32
 }
 sku: {
  name: 'B_Standard_B1ms'
  tier: 'Burstable'
 }
 backup: {
  retention: '7'
  geoRedundantBackup: 'Disabled'
 }
 // The burstable tier cannot be zone-redundant.
 highAvailability: { mode: 'Disabled' }
 maintenanceWindow: {
  dayOfWeek: 0
  startHour: 3
 }
 }
}

// "Allow Azure services" is broad: any Azure resource on the internet may try to
// connect. It is set because the Container Apps environment has no stable
// outbound IP to pin a narrower rule to.
resource pgFirewall 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2022-09-01' = if (dataMode == 'managed') {
  parent: pg
  name: 'allow-azure-services'
  properties: {
   startIpAddress: '0.0.0.0'
   endIpAddress: '255.255.255.255'
  }
}

resource pgDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2022-09-01' = if (dataMode == 'managed') {
  parent: pg
  name: namePrefix
  properties: { charset: 'UTF8' }
}

resource redis 'Microsoft.Cache/Redis@2022-05-01' = if (dataMode == 'managed') {
  name: redisName
  location: location
  properties: {
 sku: {
  name: 'C0'
  family: 'C'
  capacity: 0
 }
 enableNonSslPort: false
 minimumTlsVersion: '1.2'
 redisVersion: '6'
 publicNetworkAccess: 'Enabled'
  // The schema marks accessKeys read-only, but ARM honours it on create, and
  // the Basic/Standard tiers have no Entra-only mode to fall back on. deploy.sh
  // reads the key back and aborts if it did not stick.
  accessKeys: {
   primaryKey: redisPassword
  }
 }
}

output environmentName string = env.name
output environmentDefaultDomain string = env.properties.defaultDomain
output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output dataMode string = dataMode
// Empty in demo mode. deploy.sh reads demo.bicep's outputs for that case instead,
// which keeps the connection-string wiring in one place rather than here.
// null-forgiving: ARM cannot prove the resources exist even though the guard
// matches their deploy condition. The ternary already guarantees only the taken
// branch is evaluated, so this cannot fail in managed mode.
output postgresFqdn string = dataMode == 'managed' ? pg!.properties.fullyQualifiedDomainName : ''
output redisHost string = dataMode == 'managed' ? redis!.properties.hostName : ''
output redisPort int = dataMode == 'managed' ? redis!.properties.port : 6379
output workspaceName string = log.name
