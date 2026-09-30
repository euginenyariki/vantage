// Platform resources for Vantage. Deployed by main.bicep into the resource
// group; see that file for the subscription-scope wrapper.
//
// Data stores are reachable over public IPs so the Container Apps can connect
// without a VNet. Postgres is password-authenticated, Redis uses an access key.
// That is a demo-grade network posture: the honest upgrade is a private endpoint
// plus environment VNet integration. See docs/known-gaps.md.

@description('Deployment region.')
param location string

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
resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2022-09-01' = {
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
resource pgFirewall 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2022-09-01' = {
  parent: pg
  name: 'allow-azure-services'
  properties: {
  startIpAddress: '0.0.0.0'
  endIpAddress: '255.255.255.255'
 }
}

resource pgDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2022-09-01' = {
  parent: pg
  name: namePrefix
  properties: { charset: 'UTF8' }
}

resource redis 'Microsoft.Cache/Redis@2022-05-01' = {
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
output postgresFqdn string = pg.properties.fullyQualifiedDomainName
output redisHost string = redis.properties.hostName
output redisPort int = redis.properties.port
output workspaceName string = log.name
