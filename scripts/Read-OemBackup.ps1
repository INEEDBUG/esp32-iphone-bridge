[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Paths.ps1')
$project = Split-Path -Parent $PSScriptRoot
$data = Resolve-PhoneLabDataRoot
$identityFile = Join-Path $data 'private\board-identity.json'
$baseline = Join-Path $data 'oem-backups\baseline-20261013-01'
$python = Join-Path $project '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $identityFile)) { throw 'Confirmed board binding is missing; do not probe arbitrary serial ports.' }
$identity = Get-Content -LiteralPath $identityFile -Raw | ConvertFrom-Json
Push-Location -LiteralPath $project
try {
    if (Test-Path -LiteralPath (Join-Path $baseline 'manifest.json')) {
        & $python -m phonelab restore-preview (Join-Path $baseline 'manifest.json')
    } else {
        # Never run this while another task is holding the board. Lock/identity checks occur inside Python.
        & $python -m phonelab read-backup --port $identity.DeviceID --confirmed-instance $identity.PNPDeviceID --output $baseline *> (Join-Path $data 'private\manual-backup.log')
    }
    if ($LASTEXITCODE -ne 0) { throw 'Backup/validation failed; inspect the private task log, preserve all evidence.' }
} finally { Pop-Location }
