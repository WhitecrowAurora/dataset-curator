$ErrorActionPreference = 'Continue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$urls = @(
    'https://mirrors.huaweicloud.com/python/3.12.10/python-3.12.10-amd64.exe',
    'https://mirrors.aliyun.com/python-release/windows/python-3.12.10-amd64.exe',
    'https://pypi.tuna.tsinghua.edu.cn/simple/pyarrow/',
    'https://mirrors.aliyun.com/pypi/simple/pyarrow/'
)
foreach ($u in $urls) {
    try {
        $r = Invoke-WebRequest -Uri $u -Method Head -TimeoutSec 20 -UseBasicParsing
        "OK " + $r.StatusCode + " " + $r.Headers['Content-Length'] + " " + $u
    } catch {
        "ERR " + $_.Exception.Message.Substring(0, [Math]::Min(60, $_.Exception.Message.Length)) + " " + $u
    }
}
"WINGET=" + [bool](Get-Command winget -ErrorAction SilentlyContinue)
