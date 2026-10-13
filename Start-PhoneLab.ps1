[CmdletBinding()]
param(
    [switch]$InspectOnly,
    [switch]$WaitForDevice,
    [switch]$CaptureUsb,
    [int]$Port = 8766
)
$ErrorActionPreference = 'Stop'
if ($CaptureUsb -and $InspectOnly) {
    throw '-CaptureUsb cannot be combined with -InspectOnly.'
}
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    throw 'Project .venv is missing. Install requirements.lock.txt in a project-local venv first.'
}
Push-Location -LiteralPath $PSScriptRoot
try {
    if ($InspectOnly) {
        & $projectPython -m phonelab status --refresh
    } elseif ($CaptureUsb -and $WaitForDevice) {
        & $projectPython -m phonelab serve --port $Port --capture-usb --wait-for-device
    } elseif ($CaptureUsb) {
        & $projectPython -m phonelab serve --port $Port --capture-usb
    } elseif ($WaitForDevice) {
        & $projectPython -m phonelab serve --port $Port --wait-for-device
    } else {
        & $projectPython -m phonelab serve --port $Port
    }
    if ($LASTEXITCODE -ne 0) { throw "PhoneLab exited with code $LASTEXITCODE" }
} finally {
    Pop-Location
}
