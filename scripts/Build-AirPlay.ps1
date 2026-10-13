[CmdletBinding()]
param(
    [string]$DataRoot = '',
    [int]$BuildJobs = 4,
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Paths.ps1')
if ([string]::IsNullOrWhiteSpace($DataRoot)) { $DataRoot = Join-Path (Resolve-PhoneLabDataRoot) 'airplay-build' }
$UxPlayCommit = 'ec203121355220dd90278a369eeabb0b8ba4f14a'
$UxPlayRepository = 'https://github.com/FDH2/UxPlay.git'
$MsysRelease = '2026-09-27'
$MsysAsset = "msys2-base-x86_64-$MsysRelease.sfx.exe"
$MsysReleaseUrl = "https://github.com/msys2/msys2-installer/releases/download/2026-09-27/$MsysAsset"
$Packages = @(
    'mingw-w64-ucrt-x86_64-cmake',
    'mingw-w64-ucrt-x86_64-gcc',
    'mingw-w64-ucrt-x86_64-ninja',
    'mingw-w64-ucrt-x86_64-pkgconf',
    'mingw-w64-ucrt-x86_64-libplist',
    'mingw-w64-ucrt-x86_64-gstreamer',
    'mingw-w64-ucrt-x86_64-gst-plugins-base',
    'mingw-w64-ucrt-x86_64-gst-libav',
    'mingw-w64-ucrt-x86_64-gst-plugins-good',
    'mingw-w64-ucrt-x86_64-gst-plugins-bad'
)

if ($BuildJobs -lt 1 -or $BuildJobs -gt 32) {
    throw 'BuildJobs must be between 1 and 32.'
}
if (-not [IO.Path]::IsPathRooted($DataRoot) -or $DataRoot -match '[^\x20-\x7e]') {
    throw 'DataRoot must be an absolute ASCII path.'
}

$DataRoot = [IO.Path]::GetFullPath($DataRoot)
New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null
$MsysOuter = Join-Path $DataRoot 'msys2'
$MsysRoot = Join-Path $MsysOuter 'msys64'
$Bash = Join-Path $MsysRoot 'usr\bin\bash.exe'
$Source = Join-Path $DataRoot 'uxplay-src'
$Build = Join-Path $DataRoot 'uxplay-build'
$Exe = Join-Path $Build 'uxplay.exe'
$PrivateDataRoot = Join-Path (Split-Path $DataRoot -Parent) 'private'
$PhoneFramesPath = Join-Path $PrivateDataRoot 'airplay-phone-frames'
$OfflineValidationPath = Join-Path $PrivateDataRoot 'airplay-offline-validation'
$Archive = Join-Path $DataRoot $MsysAsset
$ChecksumFile = "$Archive.sha256"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'Git is required to fetch the pinned UxPlay source; no Git installation was found on PATH.'
}

if (-not (Test-Path -LiteralPath $Bash -PathType Leaf)) {
    if (Test-Path -LiteralPath $MsysOuter) {
        throw "MSYS2 target exists but is incomplete; inspect it before retrying: $MsysOuter"
    }

    if (-not (Test-Path -LiteralPath $Archive -PathType Leaf)) {
        Invoke-WebRequest -Uri $MsysReleaseUrl -OutFile $Archive
    }
    if (-not (Test-Path -LiteralPath $ChecksumFile -PathType Leaf)) {
        Invoke-WebRequest -Uri "$MsysReleaseUrl.sha256" -OutFile $ChecksumFile
    }

    $ExpectedHash = (Get-Content -LiteralPath $ChecksumFile -Raw).Trim().Split()[0].ToLowerInvariant()
    if ($ExpectedHash -notmatch '^[0-9a-f]{64}$') {
        throw 'The official MSYS2 checksum sidecar did not contain a SHA-256 digest.'
    }
    $ActualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Archive).Hash.ToLowerInvariant()
    if ($ActualHash -ne $ExpectedHash) {
        throw "MSYS2 archive SHA-256 mismatch. Expected $ExpectedHash, received $ActualHash."
    }
    $Signature = Get-AuthenticodeSignature -LiteralPath $Archive
    if ($Signature.Status -ne 'Valid' -or $Signature.SignerCertificate.Subject -notmatch 'Christoph Reiter') {
        throw 'MSYS2 archive Authenticode signature is invalid or has an unexpected signer.'
    }

    New-Item -ItemType Directory -Path $MsysOuter | Out-Null
    $ExtractLog = Join-Path $DataRoot 'msys2-extract.log'
    & $Archive x -y "-o$MsysOuter" *> $ExtractLog
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Bash -PathType Leaf)) {
        throw "MSYS2 base archive extraction failed; see $ExtractLog"
    }
}

# First login initializes only the extracted tree's home and keyring.
& $Bash -lc 'true' *> (Join-Path $DataRoot 'msys2-first-run.log')
if ($LASTEXITCODE -ne 0) {
    throw 'MSYS2 first-run initialization failed.'
}

function Invoke-Msys([string]$Command, [string]$LogPath) {
    & $Bash -lc "export MSYSTEM=UCRT64; export PATH=/ucrt64/bin:/usr/bin:`$PATH; $Command" *> $LogPath
    if ($LASTEXITCODE -ne 0) {
        throw "MSYS2 command failed ($LASTEXITCODE); see $LogPath"
    }
}

$PackageArgs = $Packages -join ' '
if ($SkipDependencyInstall) {
    # Cached verification path: do not refresh repositories or modify packages.
    Invoke-Msys "pacman -Q $PackageArgs" (Join-Path $DataRoot 'msys2-packages-check.log')
} else {
    # MSYS2 may close its own processes while updating the runtime, so update and
    # install are separate shell invocations. pacman verifies its signed packages.
    Invoke-Msys 'pacman -Syu --noconfirm' (Join-Path $DataRoot 'msys2-update.log')
    Invoke-Msys "pacman -S --needed --noconfirm $PackageArgs" (Join-Path $DataRoot 'msys2-packages.log')
}

if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
    & git clone --no-checkout $UxPlayRepository $Source
    if ($LASTEXITCODE -ne 0) { throw 'UxPlay source clone failed.' }
}
$CurrentCommit = (& git -C $Source rev-parse HEAD).Trim()
if ($CurrentCommit -ne $UxPlayCommit) {
    $SourceStatus = & git -C $Source status --porcelain
    if ($SourceStatus) {
        throw "UxPlay source tree has local changes; refusing to overwrite it: $Source"
    }
    & git -C $Source checkout --detach $UxPlayCommit
    if ($LASTEXITCODE -ne 0) { throw 'Could not select the pinned UxPlay commit.' }
}
if ((& git -C $Source rev-parse HEAD).Trim() -ne $UxPlayCommit) {
    throw 'UxPlay source did not resolve to the pinned commit.'
}

$SourceUnix = (& $Bash -lc "cygpath -u '$Source'").Trim()
$BuildUnix = (& $Bash -lc "cygpath -u '$Build'").Trim()
$ConfigureAndBuild = "export MSYSTEM=UCRT64; export PATH=/ucrt64/bin:/usr/bin:`$PATH; cmake -S '$SourceUnix' -B '$BuildUnix' -G Ninja -DCMAKE_BUILD_TYPE=Release -DNO_MARCH_NATIVE=ON -DNO_X11_DEPS=ON && cmake --build '$BuildUnix' --parallel $BuildJobs"
Invoke-Msys $ConfigureAndBuild (Join-Path $DataRoot 'uxplay-build.log')
if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) {
    throw 'Build returned success but uxplay.exe is missing.'
}

# -h exits in the CLI option parser; this confirms the executable and its DLL
# dependencies load without starting the receiver or opening network listeners.
$HelpLog = Join-Path $DataRoot 'uxplay-help.txt'
Invoke-Msys "cd '$BuildUnix' && ./uxplay.exe -h" $HelpLog
if (-not (Select-String -LiteralPath $HelpLog -SimpleMatch 'Usage:' -Quiet)) {
    throw "UxPlay help check did not produce a usage banner; see $HelpLog"
}

$PackageInventoryUnix = & $Bash -lc 'export MSYSTEM=UCRT64; export PATH=/ucrt64/bin:/usr/bin:$PATH; pacman -Q'
$PackageInventory = $PackageInventoryUnix -join "`n"
$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Exe).Hash
$Manifest = [ordered]@{
    builtAt = (Get-Date).ToUniversalTime().ToString('o')
    status = 'BUILD_OK_CLI_HELP_OK_SYNTHETIC_JPEG_PIPELINE_TESTED_PHONE_CAPTURE_NOT_TESTED'
    executable = $Exe
    sha256 = $Hash
    uxplayRepository = $UxPlayRepository
    uxplayCommit = $UxPlayCommit
    uxplayLicense = 'GPL-3.0-or-later (see source LICENSE)'
    buildMode = 'Windows UCRT64; internal mDNS; Release; NO_MARCH_NATIVE; no X11 dependencies'
    dependencies = $Packages
    packageInventory = $PackageInventory
    privateOutputPaths = [ordered]@{
        phoneFrames = $PhoneFramesPath
        offlineValidation = $OfflineValidationPath
    }
    captureTested = $false
    syntheticJpegPipelineTested = $true
    receiverStarted = $false
    networkOrFirewallChanged = $false
}
$ManifestPath = Join-Path $DataRoot 'build-manifest.json'
$Manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ManifestPath -Encoding utf8
$Hash | Set-Content -LiteralPath "$Exe.sha256" -Encoding ascii

Write-Output "Build status: $($Manifest.status)"
Write-Output "Executable: $Exe"
Write-Output "SHA-256: $Hash"
Write-Output "Manifest: $ManifestPath"
