#Requires -RunAsAdministrator
param(
    [string]$LocalAddress = '192.168.50.85',
    [string]$RemoteSubnet = '192.168.50.0/24',
    [string]$InterfaceAlias = 'WLAN'
)
$ErrorActionPreference = 'Stop'
$node = (Get-Command node -ErrorAction Stop).Source
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$livekit = Join-Path $root 'tools/livekit-server.exe'
if (-not (Test-Path $livekit)) { throw 'LiveKit executable was not found.' }
$specs = @(
    @{ name = 'RikumoRoom-Dev-Web-WLAN'; program = $node; protocol = 'TCP'; ports = @(5173,5174) },
    @{ name = 'RikumoRoom-Dev-Media-TCP'; program = $livekit; protocol = 'TCP'; ports = @(7881) },
    @{ name = 'RikumoRoom-Dev-Media-UDP'; program = $livekit; protocol = 'UDP'; ports = @(7882) }
)
foreach ($spec in $specs) {
    $parameters = @{
        Name = $spec.name; Direction = 'Inbound'; Action = 'Allow'; Enabled = 'True'
        Protocol = $spec.protocol; LocalPort = $spec.ports; LocalAddress = $LocalAddress
        RemoteAddress = $RemoteSubnet; InterfaceAlias = $InterfaceAlias
        Profile = @('Public','Private'); Program = $spec.program
    }
    if (Get-NetFirewallRule -Name $spec.name -ErrorAction SilentlyContinue) {
        Set-NetFirewallRule @parameters
    } else {
        New-NetFirewallRule @parameters -DisplayName $spec.name | Out-Null
    }
}
Write-Host "已允许当前局域网访问网页、证书下载和语音媒体端口。"
Write-Host "网页：https://${LocalAddress}:5173/"
Write-Host "手机证书：http://${LocalAddress}:5174/"