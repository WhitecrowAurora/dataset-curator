$ErrorActionPreference = 'Continue'
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
foreach ($p in @("$env:USERPROFILE\.cache\huggingface\token", 'K:\anlia\hf_token_write.txt')) {
    if (Test-Path $p) {
        $t = ([IO.File]::ReadAllText($p)).Trim()
        $id = if ($t.Length -gt 8) { $t.Substring(0, 3) + '...' + $t.Substring($t.Length - 4) } else { 'short' }
        "$p -> $id (len=$($t.Length))"
    } else { "$p -> MISSING" }
}
