$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $root '.dev/pids.json'
if (-not (Test-Path $pidFile)) {
    Write-Host '没有记录到运行中的开发服务。'
    exit 0
}

$records = ConvertFrom-Json -InputObject (Get-Content $pidFile -Raw)
if ($records -isnot [System.Array]) { $records = @($records) }
foreach ($record in $records) {
    $process = Get-Process -Id $record.id -ErrorAction SilentlyContinue
    if ($process -and $process.Path -eq $record.path) {
        if (-not $record.started -or $process.StartTime.ToUniversalTime().Ticks -ne [long]$record.started) {
            throw "跳过 PID $($record.id)：进程启动时间与本项目记录不符，停止发布以避免误关其他进程。"
        }
        # Vite spawns esbuild, which can keep the executable locked after Node exits.
        $dependencyRoot = [IO.Path]::GetFullPath((Join-Path $root 'web/node_modules')) + [IO.Path]::DirectorySeparatorChar
        $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($record.id)" |
            Where-Object {
                $_.Name -eq 'esbuild.exe' -and $_.ExecutablePath -and
                $_.ExecutablePath.StartsWith($dependencyRoot, [StringComparison]::OrdinalIgnoreCase)
            })
        Stop-Process -Id $record.id -Force
        if (-not $process.WaitForExit(5000)) { throw "$($record.label) 未能及时停止。" }
        foreach ($child in $children) {
            $childProcess = Get-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
            if ($childProcess -and $childProcess.Path -eq $child.ExecutablePath -and
                [Math]::Abs($childProcess.StartTime.ToUniversalTime().Ticks - $child.CreationDate.ToUniversalTime().Ticks) -lt 10000) {
                Stop-Process -Id $child.ProcessId -Force
                if (-not $childProcess.WaitForExit(5000)) { throw '本项目 esbuild 未能及时停止。' }
            }
        }
        Write-Host "$($record.label) 已停止。"
    } elseif ($process) {
        throw "跳过 PID $($record.id)：进程路径与本项目启动记录不符，停止发布以避免误关其他进程。"
    }
}
Remove-Item $pidFile -Force
