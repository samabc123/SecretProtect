$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$State = Join-Path $Root "platform\data\runtime\services.json"
$Entry = Join-Path $PSScriptRoot "service.py"
if (-not (Test-Path -LiteralPath $State)) { Write-Output "No managed services."; exit 0 }
foreach ($record in @(Get-Content -LiteralPath $State -Raw | ConvertFrom-Json)) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($record.pid)" -ErrorAction SilentlyContinue
    if (-not $process) { continue }
    if (-not $process.CommandLine.Contains($Entry) -or -not $process.CommandLine.Contains($record.service)) {
        throw "PID identity changed; refusing to stop $($record.pid)"
    }
    $expected = if ($record.started -is [DateTime]) {
        $record.started.ToUniversalTime()
    } else {
        [DateTime]::Parse($record.started, $null, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
    }
    if ([Math]::Abs(($process.CreationDate.ToUniversalTime() - $expected).TotalSeconds) -gt 2) {
        throw "PID start time changed; refusing to stop $($record.pid)"
    }
    # Windows venv python.exe can keep a real interpreter child.
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($record.pid)"
    foreach ($child in $children) {
        if ($child.CommandLine -and $child.CommandLine.Contains($Entry) -and $child.CommandLine.Contains($record.service)) {
            Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
        }
    }
    Stop-Process -Id $record.pid -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath $State
Write-Output "SecertProtect web and worker stopped."
