$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$toolsDir = Join-Path $root 'tools'
$target = Join-Path $toolsDir 'livekit-server.exe'
$download = Join-Path ([System.IO.Path]::GetTempPath()) ("livekit-{0}.zip" -f [guid]::NewGuid())

$release = Invoke-RestMethod -Uri 'https://api.github.com/repos/livekit/livekit/releases/latest' -Headers @{ 'User-Agent' = 'RikumoRoom-dev-setup' }
$asset = $release.assets | Where-Object { $_.name -match '_windows_amd64\.zip$' } | Select-Object -First 1
if (-not $asset) { throw "No Windows amd64 LiveKit archive was found in release $($release.tag_name)." }

New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
try {
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $download
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($download)
    try {
        $entry = $archive.Entries | Where-Object { $_.Name -eq 'livekit-server.exe' } | Select-Object -First 1
        if (-not $entry) { throw 'The LiveKit archive did not contain livekit-server.exe.' }
        if (Test-Path $target) { Remove-Item -LiteralPath $target -Force }
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target)
    }
    finally {
        $archive.Dispose()
    }
}
finally {
    Remove-Item -LiteralPath $download -Force -ErrorAction SilentlyContinue
}

Write-Host "Installed LiveKit Server $($release.tag_name) to $target"
