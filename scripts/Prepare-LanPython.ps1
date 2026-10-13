[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
. (Join-Path $PSScriptRoot 'Paths.ps1')
$DataRoot = Resolve-PhoneLabDataRoot
$PythonSource = Resolve-PhoneLabPythonSource
$SourceRoot = if (Test-Path -LiteralPath $PythonSource -PathType Container) { $PythonSource } else { Split-Path -Parent $PythonSource }
$TargetRoot = Join-Path $DataRoot 'toolchain\lan-python'
$SourceBasePython = if (Test-Path -LiteralPath $PythonSource -PathType Leaf) { $PythonSource } else { Join-Path $SourceRoot 'python.exe' }
$ExpectedPythonSigner = 'CN=Python Software Foundation'
$ExpectedMicrosoftSigner = 'CN=Microsoft Windows Software Compatibility Publisher'

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-Signature([string]$Path, [string]$SubjectPrefix) {
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne 'Valid' -or
        -not $signature.SignerCertificate.Subject.StartsWith($SubjectPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Trusted signer verification failed for a required runtime file."
    }
    return $signature.SignerCertificate.Subject
}

if (-not (Test-Path -LiteralPath $SourceBasePython -PathType Leaf)) {
    throw 'The installed signed Python runtime is unavailable.'
}
$sourceVersion = & $SourceBasePython -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
if ($LASTEXITCODE -ne 0 -or $sourceVersion.Trim() -notmatch '^3\.13\.') {
    throw 'The installed Python runtime must be version 3.13.'
}
if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot 'Lib\encodings') -PathType Container) -or
    -not (Test-Path -LiteralPath (Join-Path $SourceRoot 'DLLs') -PathType Container)) {
    throw 'The installed Python standard library is incomplete.'
}
if (-not (Test-Path -LiteralPath $TargetRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $TargetRoot | Out-Null
}
$targetItem = Get-Item -LiteralPath $TargetRoot -Force
if (($targetItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'The private runtime target cannot be a reparse point.'
}

$files = @('python.exe', 'python313.dll', 'python3.dll')
$files += @(Get-ChildItem -LiteralPath $SourceRoot -File -Filter 'vcruntime140*.dll' |
    Sort-Object Name | ForEach-Object Name)
if ($files -notcontains 'vcruntime140.dll' -or $files -notcontains 'vcruntime140_1.dll') {
    throw 'Required Microsoft Visual C runtime files are missing.'
}
$manifestFiles = [ordered]@{}
foreach ($name in $files) {
    $source = Join-Path $SourceRoot $name
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw 'A required signed runtime file is missing.' }
    $signerPrefix = if ($name -like 'vcruntime140*.dll') { $ExpectedMicrosoftSigner } else { $ExpectedPythonSigner }
    $signer = Assert-Signature $source $signerPrefix
    $sourceHash = Get-Sha256 $source
    $target = Join-Path $TargetRoot $name
    if (Test-Path -LiteralPath $target -PathType Leaf) {
        if ((Get-Sha256 $target) -ne $sourceHash) {
            throw "Existing private runtime file differs; refusing to overwrite."
        }
    } elseif (Test-Path -LiteralPath $target) {
        throw 'A runtime destination exists but is not a regular file.'
    } else {
        Copy-Item -LiteralPath $source -Destination $target
    }
    if ((Get-Sha256 $target) -ne $sourceHash) { throw 'Copied runtime file digest mismatch.' }
    $targetSigner = Assert-Signature $target $signerPrefix
    if ($targetSigner -ne $signer) { throw 'Copied runtime signer differs from source signer.' }
    $manifestFiles[$name] = [ordered]@{ sha256 = $sourceHash; signer = $signer }
}

# The isolated interpreter reads only these exact standard-library and project paths.
# No "import site" line is included, so global/user site-packages stay disabled.
$pthPath = Join-Path $TargetRoot 'python._pth'
$pthText = @(
    (Join-Path $SourceRoot 'DLLs'),
    (Join-Path $SourceRoot 'Lib'),
    $ProjectRoot
) -join "`n"
$pthText += "`n"
if (Test-Path -LiteralPath $pthPath -PathType Leaf) {
    if ([IO.File]::ReadAllText($pthPath) -cne $pthText) {
        throw 'Existing isolated Python path configuration differs; refusing overwrite.'
    }
} elseif (Test-Path -LiteralPath $pthPath) {
    throw 'The isolated Python path configuration target is not a regular file.'
} else {
    [IO.File]::WriteAllText($pthPath, $pthText, [Text.UTF8Encoding]::new($false))
}
if ([IO.File]::ReadAllText($pthPath).Contains('import site')) { throw 'Global site initialization is forbidden.' }

$python = Join-Path $TargetRoot 'python.exe'
$check = @'
import importlib.util, json, pathlib, sys
import phonelab.lan_lab
root = pathlib.Path(sys.argv[1]).resolve()
assert pathlib.Path(sys.executable).resolve() == root / "python.exe"
assert sys.version_info[:2] == (3, 13)
assert sys.flags.no_site == 1
assert importlib.util.find_spec("phonelab.lan_lab") is not None
assert any(pathlib.Path(p).resolve() == pathlib.Path(sys.argv[2]).resolve() for p in sys.path if p)
print(json.dumps({"ready": True, "version": sys.version.split()[0]}, separators=(",", ":")))
'@
$result = & $python -c $check $TargetRoot $ProjectRoot
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($result -join ''))) {
    throw 'The isolated Python import contract did not pass.'
}
try { $runtimeCheck = ($result -join '') | ConvertFrom-Json } catch { throw 'The isolated Python check returned invalid data.' }
if ($runtimeCheck.ready -ne $true) { throw 'The isolated Python check did not report ready.' }

$manifest = [ordered]@{
    schema = 1
    python_version = [string]$runtimeCheck.version
    files = $manifestFiles
    python_pth_sha256 = Get-Sha256 $pthPath
}
$manifestPath = Join-Path $TargetRoot 'runtime-manifest.json'
$manifestText = ($manifest | ConvertTo-Json -Depth 5) + "`n"
if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
    if ([IO.File]::ReadAllText($manifestPath) -cne $manifestText) {
        throw 'Existing runtime manifest differs; refusing overwrite.'
    }
} elseif (Test-Path -LiteralPath $manifestPath) {
    throw 'The runtime manifest target is not a regular file.'
} else {
    [IO.File]::WriteAllText($manifestPath, $manifestText, [Text.UTF8Encoding]::new($false))
}

Write-Output "PRIVATE_LAN_PYTHON_READY version=$($runtimeCheck.version)"
foreach ($name in $files) { Write-Output "$name sha256=$($manifestFiles[$name].sha256) signer=verified" }
Write-Output "python._pth sha256=$(Get-Sha256 $pthPath)"
