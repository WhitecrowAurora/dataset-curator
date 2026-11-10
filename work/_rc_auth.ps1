$ErrorActionPreference = 'Continue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$proxy = 'http://127.0.0.1:7897'
$tok = ([IO.File]::ReadAllText("$env:USERPROFILE\.cache\huggingface\token")).Trim()
$hdr = @{ Authorization = "Bearer $tok" }

try {
    $r = Invoke-WebRequest -Uri 'https://huggingface.co/api/whoami-v2' -Proxy $proxy -Headers $hdr -TimeoutSec 20 -UseBasicParsing
    "WHOAMI=" + $r.StatusCode
    $j = $r.Content | ConvertFrom-Json
    "USER=" + $j.name + " TYPE=" + $j.type
    "ORGS=" + (($j.orgs | ForEach-Object { $_.name }) -join ',')
} catch { "WHOAMI_ERR=" + $_.Exception.Message.Substring(0, [Math]::Min(80, $_.Exception.Message.Length)) }

# preupload 只做权限检查，不落文件
$repo = 'MatchaCuratio/NewBie-Training-XML-Caption'
$body = @{ files = @(@{ path = 'probe_permission.txt' }) } | ConvertTo-Json -Depth 5 -Compress
foreach ($rt in @('datasets', 'models')) {
    try {
        $r2 = Invoke-WebRequest -Uri ("https://huggingface.co/api/{0}/{1}/preupload/main" -f $rt, $repo) `
              -Method Post -Proxy $proxy -Headers $hdr -ContentType 'application/json' -Body $body -TimeoutSec 20 -UseBasicParsing
        "PREUPLOAD_" + $rt + "=" + $r2.StatusCode + " " + $r2.Content.Substring(0, [Math]::Min(120, $r2.Content.Length))
        break
    } catch {
        "PREUPLOAD_" + $rt + "_ERR=" + $_.Exception.Message.Substring(0, [Math]::Min(80, $_.Exception.Message.Length))
    }
}
