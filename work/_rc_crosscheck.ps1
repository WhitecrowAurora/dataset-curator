$ErrorActionPreference = 'Continue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
$proxy = 'http://127.0.0.1:7897'
$ua = @{ 'User-Agent' = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36' }

$ids = 2588000, 5545000, 1388000, 7541000, '3122376', '4458376'
foreach ($id in $ids) {
    try {
        $r = Invoke-WebRequest -Uri ("https://danbooru.donmai.us/posts/{0}.json" -f $id) `
             -Proxy $proxy -Headers $ua -TimeoutSec 25 -UseBasicParsing
        $j = $r.Content | ConvertFrom-Json
        $art = $j.tag_string_artist
        "ID={0} status={1} artist={2}" -f $id, $r.StatusCode, ($art -replace "`n", ' ')
    } catch {
        "ID={0} ERR={1}" -f $id, $_.Exception.Message.Substring(0, [Math]::Min(60, $_.Exception.Message.Length))
    }
}
