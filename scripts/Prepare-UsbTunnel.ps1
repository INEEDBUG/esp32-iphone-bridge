[CmdletBinding()]
param(
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$PatchHelper = Join-Path $PSScriptRoot 'usb_tunnel_patch.py'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'Project-local Python environment is unavailable.'
}
$Version = & $Python -c "import importlib.metadata as m; print(m.version('pymobiledevice3'))"
if ($LASTEXITCODE -ne 0 -or $Version.Trim() -ne '11.29.0') {
    throw 'Project-local pymobiledevice3 version is not the pinned 11.29.0.'
}
$Source = & $Python -c "import importlib.util as u; s=u.find_spec('pymobiledevice3.remote.userspace_tunnel'); print(s.origin if s else '')"
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($Source)) {
    throw 'Pinned userspace tunnel source could not be resolved.'
}
$Source = [IO.Path]::GetFullPath($Source.Trim())
if (-not $Source.StartsWith((Join-Path $ProjectRoot '.venv\Lib\site-packages\pymobiledevice3\'), [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Resolved source is outside this project-local virtual environment.'
}
$Arguments = @($PatchHelper, $Source)
if ($Apply) { $Arguments += '--apply' }
& $Python @Arguments
if ($LASTEXITCODE -ne 0) {
    throw 'Pinned USB tunnel source preparation failed closed.'
}
