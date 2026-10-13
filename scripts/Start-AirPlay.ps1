[CmdletBinding(DefaultParameterSetName = 'Preview')]
param(
    [Parameter(ParameterSetName = 'Run', Mandatory = $true)]
    [switch]$Run,
    [string]$DataRoot = ''
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Paths.ps1')
if ([string]::IsNullOrWhiteSpace($DataRoot)) { $DataRoot = Join-Path (Resolve-PhoneLabDataRoot) 'airplay-build' }
$PinnedCommit = 'ec203121355220dd90278a369eeabb0b8ba4f14a'
$PinnedExecutableHash = 'DE065F3ADB58089AD5A7859359671CB143664E571A64ADE3D869D3C57736EE4E'
$DataRoot = [IO.Path]::GetFullPath($DataRoot)
$MsysRoot = Join-Path $DataRoot 'msys2\msys64'
$UxPlay = Join-Path $DataRoot 'uxplay-build\uxplay.exe'
$GstLaunch = Join-Path $MsysRoot 'ucrt64\bin\gst-launch-1.0.exe'
$ApprovalFile = Join-Path $DataRoot 'receiver-run-approval.json'
$PrivateRoot = Join-Path (Split-Path $DataRoot -Parent) 'private'
$Frames = Join-Path $PrivateRoot 'airplay-phone-frames'
$OfflineValidation = Join-Path $PrivateRoot 'airplay-offline-validation'
$Port = 57123
$AirPlayBasePort = 53100
$OutputPattern = Join-Path $Frames 'frame-%06d.jpg'

if (-not (Test-Path -LiteralPath $UxPlay -PathType Leaf) -or -not (Test-Path -LiteralPath $GstLaunch -PathType Leaf)) {
    throw 'The pinned UxPlay executable and local GStreamer runtime must exist first. Run Build-AirPlay.ps1.'
}

$UxArgs = @(
    '-p', "$AirPlayBasePort",
    '-pin',
    '-vd', 'avdec_h264',
    '-vrtp', "config-interval=1 ! udpsink host=127.0.0.1 port=$Port"
)
$UxArgumentLine = '-p 53100 -pin -n PhoneLab -nh -vd avdec_h264 -vrtp "config-interval=1 ! udpsink host=127.0.0.1 port=57123"'
$GstArgs = @(
    '-q', 'udpsrc', 'address=127.0.0.1', "port=$Port",
    '!', 'application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96',
    '!', 'rtph264depay', '!', 'h264parse', '!', 'avdec_h264',
    '!', 'videoconvert', '!', 'videorate', '!', 'video/x-raw,framerate=1/1',
    '!', 'jpegenc', '!', 'multifilesink', "location=$OutputPattern", 'max-files=60'
)

if (-not $Run) {
    Write-Output 'PREVIEW ONLY: no process or network listener has been started.'
}
Write-Output "UxPlay executable: $UxPlay"
Write-Output "UxPlay args: $UxArgumentLine"
Write-Output "Proposed AirPlay ports: TCP $AirPlayBasePort-$($AirPlayBasePort + 2), UDP $AirPlayBasePort-$($AirPlayBasePort + 2), plus UDP 5353 mDNS. Candidate ports require a read-only conflict check and approved firewall scope before any run."
Write-Output "Local RTP receiver: 127.0.0.1:$Port (within this PC only)"
Write-Output "GStreamer frame output: $OutputPattern (60 JPEG files maximum, sampled at 1 frame/second)"
Write-Output "Offline synthetic validation location: $OfflineValidation (test patterns only; not phone evidence)"
Write-Output 'UxPlay itself binds its AirPlay sockets on all interfaces in this build. A reviewed, effective program-scoped firewall confinement is required before any receiver run.'
Write-Output "Run gate: $ApprovalFile (must explicitly say approved=true AND firewallScopeVerified=true)"

if (-not $Run) { return }

if (-not (Test-Path -LiteralPath $ApprovalFile -PathType Leaf)) {
    throw "Run blocked: no approval marker exists. Preview did not start anything. Required marker: $ApprovalFile"
}
$Approval = Get-Content -LiteralPath $ApprovalFile -Raw | ConvertFrom-Json
if ($Approval.approved -ne $true -or $Approval.firewallScopeVerified -ne $true) {
    throw 'Run blocked: approval and independently verified firewall confinement are both required.'
}
if ($Approval.uxplayCommit -ne 'ec203121355220dd90278a369eeabb0b8ba4f14a') {
    throw 'Run blocked: approval marker does not match the pinned executable source.'
}
$ExpectedPorts = '53100,53101,53102'
if ($Approval.firewallProfile -ne 'Public' -or
    [string]::IsNullOrWhiteSpace([string]$Approval.interfaceAlias) -or
    [string]::IsNullOrWhiteSpace([string]$Approval.localAddress) -or
    [string]::IsNullOrWhiteSpace([string]$Approval.remoteSubnet) -or
    [IO.Path]::GetFullPath([string]$Approval.programPath) -ne [IO.Path]::GetFullPath($UxPlay) -or
    (($Approval.tcpPorts -join ',') -ne $ExpectedPorts) -or
    (($Approval.udpPorts -join ',') -ne $ExpectedPorts) -or
    [int]$Approval.mdnsUdpPort -ne 5353 -or
    [IO.Path]::GetFullPath([string]$Approval.frameOutputDirectory) -ne [IO.Path]::GetFullPath($Frames) -or
    [IO.Path]::GetFullPath([string]$Approval.offlineValidationDirectory) -ne [IO.Path]::GetFullPath($OfflineValidation)) {
    throw 'Run blocked: approval marker does not specify the exact executable, current Public profile, interface/address/subnet scope, and fixed protocol ports.'
}
$LocalAddress = $null
if (-not [Net.IPAddress]::TryParse([string]$Approval.localAddress, [ref]$LocalAddress) -or
    $LocalAddress.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
    throw 'Run blocked: marker localAddress must be a numeric IPv4 address.'
}
$RemoteParts = ([string]$Approval.remoteSubnet).Split('/')
$RemoteAddress = $null
$PrefixLength = 0
if ($RemoteParts.Count -ne 2 -or
    -not [Net.IPAddress]::TryParse($RemoteParts[0], [ref]$RemoteAddress) -or
    $RemoteAddress.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
    -not [int]::TryParse($RemoteParts[1], [ref]$PrefixLength) -or
    $PrefixLength -lt 24 -or $PrefixLength -gt 30) {
    throw 'Run blocked: marker remoteSubnet must be a canonical /24 to /30 IPv4 CIDR.'
}
$Octets = $RemoteAddress.GetAddressBytes()
$IsPrivateLan = ($Octets[0] -eq 10) -or
    ($Octets[0] -eq 172 -and $Octets[1] -ge 16 -and $Octets[1] -le 31) -or
    ($Octets[0] -eq 192 -and $Octets[1] -eq 168)
if (-not $IsPrivateLan) { throw 'Run blocked: remoteSubnet is not in RFC1918 private address space.' }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Run blocked: git is required to verify the pinned UxPlay source tree.' }
$Source = Join-Path $DataRoot 'uxplay-src'
$ActualCommit = (& git -C $Source rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $ActualCommit -ne $PinnedCommit) { throw 'Run blocked: UxPlay source commit drifted from the reviewed pin.' }
$SourceChanges = & git -C $Source status --porcelain
if ($LASTEXITCODE -ne 0 -or $SourceChanges) { throw 'Run blocked: UxPlay source tree is dirty; refusing to launch.' }
$ActualExecutableHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $UxPlay).Hash
if ($ActualExecutableHash -ne $PinnedExecutableHash) { throw 'Run blocked: UxPlay executable hash drifted from the reviewed build.' }
$ScopeVerifier = Join-Path $PSScriptRoot 'Test-AirPlayScope.ps1'
if (-not (Test-Path -LiteralPath $ScopeVerifier -PathType Leaf)) {
    throw 'Run blocked: independent live firewall scope verifier is missing.'
}
$ScopeResult = & $ScopeVerifier -DataRoot $DataRoot
if ([string]$ScopeResult -ne 'AIRPLAY_SCOPE_VERIFIED') {
    throw "Run blocked by independent live firewall scope check: $ScopeResult"
}

Write-Output 'Run gate passed. Starting foreground UxPlay receiver and local capture pipeline.'
New-Item -ItemType Directory -Force -Path $Frames | Out-Null
$PreviousPath = $env:PATH
$PreviousGstPluginPath = $env:GST_PLUGIN_PATH
$PreviousGstPluginSystemPath = $env:GST_PLUGIN_SYSTEM_PATH_1_0
$env:PATH = (Join-Path $MsysRoot 'ucrt64\bin') + ';' + (Join-Path $MsysRoot 'usr\bin') + ';' + $env:PATH
$env:GST_PLUGIN_PATH = Join-Path $MsysRoot 'ucrt64\lib\gstreamer-1.0'
$env:GST_PLUGIN_SYSTEM_PATH_1_0 = $env:GST_PLUGIN_PATH
$GstProcess = $null
$UxProcess = $null
try {
    $GstProcess = Start-Process -FilePath $GstLaunch -ArgumentList $GstArgs -PassThru -WindowStyle Hidden
    Start-Sleep -Milliseconds 700
    if ($GstProcess.HasExited) { throw "Local GStreamer RTP capture exited early with code $($GstProcess.ExitCode)." }
    $UxProcess = Start-Process -FilePath $UxPlay -ArgumentList $UxArgumentLine -PassThru -WindowStyle Normal
    Write-Output "UxPlay PID: $($UxProcess.Id). Its generated PIN is shown only in its separate local console window; no PIN is copied to this script's output or a file. Close that window or press Ctrl+C here to stop the receiver and its spawned capture process."
    $NextScopeCheck = [DateTime]::UtcNow.AddSeconds(10)
    while (-not $UxProcess.WaitForExit(1000)) {
        if ($GstProcess.HasExited) { throw 'Local capture process exited; stopping receiver.' }
        if ([DateTime]::UtcNow -ge $NextScopeCheck) {
            $LiveScope = & $ScopeVerifier -DataRoot $DataRoot
            if ([string]$LiveScope -ne 'AIRPLAY_SCOPE_VERIFIED') {
                throw 'Network or firewall scope changed; stopping receiver. Explicit revalidation is required.'
            }
            $NextScopeCheck = [DateTime]::UtcNow.AddSeconds(10)
        }
    }
    if ($UxProcess.ExitCode -ne 0) { throw "UxPlay process exited with code $($UxProcess.ExitCode)." }
}
finally {
    if ($UxProcess -and -not $UxProcess.HasExited) {
        Stop-Process -Id $UxProcess.Id -Force -ErrorAction SilentlyContinue
        $UxProcess.WaitForExit()
    }
    if ($GstProcess -and -not $GstProcess.HasExited) {
        Stop-Process -Id $GstProcess.Id -Force
        $GstProcess.WaitForExit()
    }
    $env:PATH = $PreviousPath
    $env:GST_PLUGIN_PATH = $PreviousGstPluginPath
    $env:GST_PLUGIN_SYSTEM_PATH_1_0 = $PreviousGstPluginSystemPath
}
