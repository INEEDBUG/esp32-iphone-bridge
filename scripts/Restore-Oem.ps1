[CmdletBinding()]
param([switch]$Execute)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Paths.ps1')
if ($Execute) { throw 'RESTORE_LOCKED: this entry is preview-only. Any approved failure recovery requires a reviewed target-bound writer and complete readback; it is not an automatic startup action.' }
$project = Split-Path -Parent $PSScriptRoot
$data = Resolve-PhoneLabDataRoot
Push-Location -LiteralPath $project
try {
    & (Join-Path $project '.venv\Scripts\python.exe') -m phonelab restore-preview (Join-Path $data 'oem-backups\baseline-20261013-01\manifest.json')
    if ($LASTEXITCODE -ne 0) { throw 'Backup integrity not verified; no restore was attempted.' }
} finally { Pop-Location }
