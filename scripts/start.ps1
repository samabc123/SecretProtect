param([string]$Python = "python", [int]$Port = 8001)
$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$Runtime = Join-Path $Root "platform\data\runtime"
$Entry = Join-Path $PSScriptRoot "service.py"
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    throw "Port $Port is already in use. Choose -Port explicitly."
}
New-Item -ItemType Directory -Force -Path $Runtime | Out-Null
$PythonPath = (Get-Command $Python).Source
$env:PYTHONUTF8 = "1"
$records = @()
foreach ($kind in @("web", "worker")) {
    $process = Start-Process -FilePath $PythonPath -ArgumentList @("`"$Entry`"", $kind, "--port", $Port) `
        -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $Runtime "$kind.stdout.log") `
        -RedirectStandardError (Join-Path $Runtime "$kind.stderr.log")
    $records += @{ pid = $process.Id; service = $kind; entry = $Entry; started = $process.StartTime.ToUniversalTime().ToString("o") }
}
$records | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Runtime "services.json") -Encoding utf8
Start-Sleep -Seconds 3
try {
    foreach ($record in $records) {
        if (-not (Get-Process -Id $record.pid -ErrorAction SilentlyContinue)) {
            throw "$($record.service) process exited."
        }
    }
    $response = Invoke-WebRequest "http://127.0.0.1:$Port/login" -UseBasicParsing -TimeoutSec 10
    if ($response.StatusCode -ne 200) { throw "Unexpected HTTP status" }
    Write-Output "SecertProtect: http://127.0.0.1:$Port/"
} catch {
    & (Join-Path $PSScriptRoot "stop.ps1")
    throw "Startup failed; inspect platform/data/runtime/*.stderr.log"
}
