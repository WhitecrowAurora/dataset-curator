$ErrorActionPreference = 'Continue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$out = @()
foreach ($p in @('K:\anlia\hf_token_write.txt', 'C:\Users\anlia\hf_token_write.txt')) {
    $exists = Test-Path $p
    $out += "$p exists=$exists"
    if ($exists) {
        $tok = ([IO.File]::ReadAllText($p)).Trim()
        $out += "  len=" + $tok.Length + " prefix=" + $tok.Substring(0, 3)
        try {
            $r = Invoke-WebRequest -Uri 'https://huggingface.co/api/whoami-v2' -Proxy 'http://127.0.0.1:7897' `
                 -Headers @{ Authorization = "Bearer $tok" } -TimeoutSec 20 -UseBasicParsing
            $j = $r.Content | ConvertFrom-Json
            $out += "  whoami=" + $j.name + " role=" + $j.auth.accessToken.role
            $out += "  scopes=" + (($j.auth.accessToken.fineGrained.scoped | ForEach-Object { $_.entity.type + ':' + $_.entity.name }) -join ',')
            $out += "  orgs=" + (($j.orgs | ForEach-Object { $_.name + '/' + $_.roleInOrg }) -join ',')
        } catch {
            $out += "  whoami_err=" + $_.Exception.Message.Substring(0, [Math]::Min(70, $_.Exception.Message.Length))
        }
    }
}
$out += "HF_ENDPOINT=" + [Environment]::GetEnvironmentVariable('HF_ENDPOINT', 'Machine')
$out += "HF_ENDPOINT_USER=" + $env:HF_ENDPOINT
$out -join "`n"
