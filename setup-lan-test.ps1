param([string]$LocalAddress = '192.168.50.85')
$ErrorActionPreference = 'Stop'
$LocalAddress = ([System.Net.IPAddress]::Parse($LocalAddress)).ToString()
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$tls = Join-Path $root '.dev/tls'
$openssl = (Get-Command openssl -ErrorAction Stop).Source
New-Item -ItemType Directory -Force -Path $tls | Out-Null
$opensslConfig = Join-Path $tls 'openssl.cnf'
"[req]`ndistinguished_name=dn`n[dn]" | Set-Content -LiteralPath $opensslConfig -Encoding ascii
function Invoke-OpenSSL([string[]]$Arguments) {
    & $openssl @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'OpenSSL certificate generation failed.' }
}
$caKey = Join-Path $tls 'ca.key'
$caCert = Join-Path $tls 'ca.crt'
if (-not (Test-Path $caCert)) {
    Invoke-OpenSSL @('req','-config',$opensslConfig,'-x509','-newkey','rsa:2048','-nodes','-sha256','-days','365',
        '-keyout',$caKey,'-out',$caCert,'-subj','/CN=Rikumo Local Test CA',
        '-addext','basicConstraints=critical,CA:TRUE','-addext','keyUsage=critical,keyCertSign,cRLSign')
}
$serverKey = Join-Path $tls 'server.key'
$request = Join-Path $tls 'server.csr'
$extension = Join-Path $tls 'server.ext'
@"
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:localhost,IP:127.0.0.1,IP:$LocalAddress
"@ | Set-Content -LiteralPath $extension -Encoding ascii
Invoke-OpenSSL @('req','-config',$opensslConfig,'-new','-newkey','rsa:2048','-nodes','-keyout',$serverKey,'-out',$request,'-subj','/CN=Rikumo Local Test')
Invoke-OpenSSL @('x509','-req','-in',$request,'-CA',$caCert,'-CAkey',$caKey,'-CAcreateserial',
    '-out',(Join-Path $tls 'server.crt'),'-days','30','-sha256','-extfile',$extension)
Invoke-OpenSSL @('x509','-in',$caCert,'-outform','DER','-out',(Join-Path $tls 'ca.cer'))
@{ localAddress = $LocalAddress } | ConvertTo-Json | Set-Content -Encoding ascii -LiteralPath (Join-Path $root '.dev/lan-test.json')
Write-Host "Test URL: https://${LocalAddress}:5173/"
Write-Host "CA download: http://${LocalAddress}:5174/"
Write-Host 'Restart development services to enable this configuration.'
