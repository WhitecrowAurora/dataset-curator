$ErrorActionPreference = 'Continue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$proxy = 'http://127.0.0.1:7897'
$tok = ([IO.File]::ReadAllText("$env:USERPROFILE\.cache\huggingface\token")).Trim()
$r = Invoke-WebRequest -Uri 'https://huggingface.co/api/whoami-v2' -Proxy $proxy `
     -Headers @{ Authorization = "Bearer $tok" } -TimeoutSec 20 -UseBasicParsing
$j = $r.Content | ConvertFrom-Json
"USER=" + $j.name
"TOKEN_NAME=" + $j.auth.accessToken.displayName
"TOKEN_ROLE=" + $j.auth.accessToken.role
"TOKEN_SCOPES=" + (($j.auth.accessToken.fineGrained.scopes) -join ',')
"CAN_CREATE_REPOS=" + $j.canCreateRepo
foreach ($o in $j.orgs) { "ORG=" + $o.name + " ROLE=" + $o.roleInOrg + " PERMS=" + ($o.permissions -join ',') }
