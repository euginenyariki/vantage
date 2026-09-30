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

module platform 'platform.bicep' = {
  name: 'vantage-platform'
  scope: resourceGroup(resourceGroupName)
  params: {
   location: location
   namePrefix: namePrefix
   postgresVersion: postgresVersion
   postgresAdminUser: postgresAdminUser
   postgresAdminPassword: postgresAdminPassword
   redisPassword: redisPassword
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
output postgresFqdn string = platform.outputs.postgresFqdn
output redisHost string = platform.outputs.redisHost
output redisPort int = platform.outputs.redisPort
output workspaceName string = platform.outputs.workspaceName
