$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$stateDir = Join-Path $root '.dev'
$logDir = Join-Path $stateDir 'logs'
$python = Join-Path $root 'server/.venv/Scripts/python.exe'
$vite = Join-Path $root 'web/node_modules/vite/bin/vite.js'
$bundledLiveKit = Join-Path $root 'tools/livekit-server.exe'
$livekitCommand = Get-Command livekit-server -ErrorAction SilentlyContinue
$livekit = if (Test-Path $bundledLiveKit) { $bundledLiveKit } elseif ($livekitCommand) { $livekitCommand.Source } else { $null }
$nodeCommand = Get-Command node -ErrorAction SilentlyContinue
$lanConfigPath = Join-Path $stateDir 'lan-test.json'
$lanAddress = $null
if (Test-Path $lanConfigPath) {
    $lanConfig = Get-Content $lanConfigPath -Raw | ConvertFrom-Json
    $lanAddress = ([System.Net.IPAddress]::Parse($lanConfig.localAddress)).ToString()
}
$livekitArguments = @('--dev', '--bind', '0.0.0.0')
if ($lanAddress) { $livekitArguments += @('--node-ip', $lanAddress) }

if (-not (Test-Path $python)) { throw 'Python dependencies are missing. Follow the one-time setup in README.md.' }
if (-not (Test-Path $vite) -or -not $nodeCommand) { throw 'Web dependencies are missing. Run npm --prefix web install first.' }
if (-not $livekit) { throw 'LiveKit Server is missing. Put livekit-server.exe in tools or add it to PATH.' }
if (Test-Path (Join-Path $stateDir 'pids.json')) { throw 'A development session is already recorded. Run ./stop-dev.ps1 first.' }
$listeners = netstat -ano -p tcp
$developmentPorts = @(7880, 8000, 5173)
if (Test-Path (Join-Path $stateDir 'tls/server.crt')) { $developmentPorts += 5174 }
$busyPorts = @($developmentPorts | Where-Object { $port = $_; $listeners | Where-Object { $_ -match ":$port\s+\S+\s+LISTENING\s+\d+\s*$" } })
if ($busyPorts.Count) { throw "These development ports are already in use: $($busyPorts -join ', '). Run ./stop-dev.ps1 or close the other service first." }

function Get-PortRecord([int]$port, [string]$label) {
    $line = $null
    for ($attempt = 0; $attempt -lt 20 -and -not $line; $attempt++) {
        $line = netstat -ano -p tcp | Where-Object { $_ -match ":$port\s+\S+\s+LISTENING\s+(\d+)\s*$" } | Select-Object -First 1
        if (-not $line) { Start-Sleep -Milliseconds 500 }
    }
    if (-not $line) { throw "$label did not open port $port." }
    $processId = [int]([regex]::Match($line, '\d+\s*$').Value)
    $process = Get-Process -Id $processId
    return @{ id = $processId; path = $process.Path; started = $process.StartTime.ToUniversalTime().Ticks; label = $label }
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$started = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
try {
    $started.Add((Start-Process -FilePath $livekit -ArgumentList $livekitArguments -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'livekit.log') -RedirectStandardError (Join-Path $logDir 'livekit-error.log') -PassThru))
    $started.Add((Start-Process -FilePath $python -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', '8000') -WorkingDirectory (Join-Path $root 'server') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'api.log') -RedirectStandardError (Join-Path $logDir 'api-error.log') -PassThru))
    $started.Add((Start-Process -FilePath $nodeCommand.Source -ArgumentList @($vite, '--host', '0.0.0.0') -WorkingDirectory (Join-Path $root 'web') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'web.log') -RedirectStandardError (Join-Path $logDir 'web-error.log') -PassThru))

    $records = @(
        (Get-PortRecord 7880 'LiveKit'),
        (Get-PortRecord 8000 'API'),
        (Get-PortRecord 5173 'Web')
    )
    $records | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $stateDir 'pids.json')
    Write-Host '璃雲の部屋开发服务已启动：'
    if ($lanAddress) {
        Write-Host "  网页： https://${lanAddress}:5173/"
        Write-Host "  证书： http://${lanAddress}:5174/"
    } else { Write-Host '  网页： http://localhost:5173/' }
    Write-Host '  API：  http://localhost:8000/api/health'
    Write-Host '  语音： ws://localhost:7880'
    Write-Host "日志：  $logDir"
}
catch {
    foreach ($process in $started) {
        if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
    }
    Remove-Item (Join-Path $stateDir 'pids.json') -Force -ErrorAction SilentlyContinue
    throw
}
