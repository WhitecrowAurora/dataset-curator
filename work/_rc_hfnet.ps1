$ErrorActionPreference = 'Continue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
foreach ($u in @('https://huggingface.co', 'https://huggingface.co/api/whoami-v2')) {
    try {
        $r = Invoke-WebRequest -Uri $u -Method Head -TimeoutSec 15 -UseBasicParsing
        "OK " + $r.StatusCode + " " + $u
    } catch {
        "ERR " + $_.Exception.Message.Substring(0, [Math]::Min(70, $_.Exception.Message.Length)) + " " + $u
    }
}
$tokPath = "$env:USERPROFILE\.cache\huggingface\token"
if (Test-Path $tokPath) {
    $tok = ([IO.File]::ReadAllText($tokPath)).Trim()
    "TOKLEN=" + $tok.Length
    try {
        $r2 = Invoke-WebRequest -Uri 'https://huggingface.co/api/whoami-v2' -Headers @{ Authorization = "Bearer $tok" } -TimeoutSec 15 -UseBasicParsing
        "WHOAMI=" + $r2.StatusCode + " " + $r2.Content.Substring(0, [Math]::Min(300, $r2.Content.Length))
    } catch {
        "WHOAMI_ERR=" + $_.Exception.Message.Substring(0, [Math]::Min(70, $_.Exception.Message.Length))
    }
} else { "NO_TOKEN_FILE" }
