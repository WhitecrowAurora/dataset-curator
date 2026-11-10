$ErrorActionPreference = 'Continue'
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8

"ENV=" + (@('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy') |
    ForEach-Object { $v = [Environment]::GetEnvironmentVariable($_) ; if ($v) { $_ + '=' + $v } }) -join ' | '

$names = 'clash','clash-verge','Clash for Windows','v2ray','v2rayN','xray','sing-box','Netch','trojan','hysteria','mihomo','verge-mihomo','nekoray','qv2ray','shadowsocks','ss-local'
"PROC=" + ((Get-Process -ErrorAction SilentlyContinue | Where-Object { $n = $_.ProcessName; $names | Where-Object { $n -like ("*" + $_ + "*") } } | Select-Object -First 10 | ForEach-Object { $_.ProcessName }) -join ', ')

$ports = 7890, 7891, 7897, 1080, 1081, 10808, 10809, 20171, 2080, 8080, 8889, 33210
$open = @()
foreach ($p in $ports) {
    $c = New-Object Net.Sockets.TcpClient
    try {
        $iar = $c.BeginConnect('127.0.0.1', $p, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne(300) -and $c.Connected) { $open += $p }
    } catch { } finally { $c.Close() }
}
"OPEN_PORTS=" + ($open -join ', ')

foreach ($p in $open) {
    foreach ($scheme in @('http', 'socks5')) {
        try {
            $r = Invoke-WebRequest -Uri 'https://huggingface.co/api/whoami-v2' -Proxy ("{0}://127.0.0.1:{1}" -f $scheme, $p) `
                 -Headers @{ Authorization = 'Bearer invalid' } -TimeoutSec 12 -UseBasicParsing
            "$scheme`:$p STATUS=" + $r.StatusCode
        } catch {
            $msg = $_.Exception.Message
            if ($msg -match '401|403') { "$scheme`:$p REACHED_AUTH_" + ($msg -replace '\D', '').Substring(0, 3) }
            else { "$scheme`:$p ERR=" + $msg.Substring(0, [Math]::Min(45, $msg.Length)) }
        }
    }
}
