[CmdletBinding()]
param([switch]$Run)

$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
. (Join-Path $PSScriptRoot 'Paths.ps1')
$Python = Join-Path (Resolve-PhoneLabDataRoot) 'toolchain\lan-python\python.exe'

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    if ($Run) { throw 'LAN run blocked: first prepare the isolated project Python runtime.' }
    Write-Output 'PREVIEW_ONLY: isolated runtime not prepared; no process or listener started.'
    return
}

$Arguments = @('-m', 'phonelab.lan_cli')
if ($Run) { $Arguments += '--run' }
& $Python @Arguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
