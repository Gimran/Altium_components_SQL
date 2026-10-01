# Stop the web UI left running on the configured port.
# Only a python process running app\main.py is stopped; anything else on the port is reported.
$ErrorActionPreference = 'Stop'
$config = Join-Path $PSScriptRoot 'config.json'
$port = 8777
if (Test-Path $config) {
    $cfg = Get-Content $config -Raw | ConvertFrom-Json
    if ($cfg.port) { $port = [int]$cfg.port }
}

$owners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique)
if (-not $owners) {
    Write-Output "Port ${port}: nothing running."
    exit 0
}
foreach ($id in $owners) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$id"
    if ($p -and $p.CommandLine -like '*main.py*') {
        Stop-Process -Id $id -Force
        Write-Output "Port ${port}: stopped the web UI (PID $id)."
    } else {
        Write-Output "Port ${port}: used by $($p.Name) (PID $id), not the web UI - left alone."
        exit 1
    }
}
# Give Windows a moment to release the socket.
for ($i = 0; $i -lt 20 -and (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue); $i++) {
    Start-Sleep -Milliseconds 200
}
exit 0
