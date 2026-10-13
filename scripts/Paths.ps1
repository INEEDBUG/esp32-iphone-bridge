# Shared local path resolution. .phonelab-local.json is ignored and contains paths only.
$script:PhoneLabProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:PhoneLabLocalConfigPath = Join-Path $script:PhoneLabProjectRoot '.phonelab-local.json'

function Get-PhoneLabLocalConfig {
    if (-not (Test-Path -LiteralPath $script:PhoneLabLocalConfigPath -PathType Leaf)) { return @{} }
    try {
        $value = Get-Content -LiteralPath $script:PhoneLabLocalConfigPath -Raw | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $value -or $value -is [array] -or $value -isnot [pscustomobject]) { throw 'invalid shape' }
        $allowed = @('data_root', 'pwsh', 'python_source')
        foreach ($property in $value.PSObject.Properties) {
            if ($property.Name -notin $allowed -or $property.Value -isnot [string] -or
                [string]::IsNullOrWhiteSpace([string]$property.Value)) { throw 'invalid path entry' }
        }
        return $value
    } catch {
        throw 'PhoneLab local path configuration is invalid.'
    }
}

function Resolve-PhoneLabDataRoot([string]$Requested = '') {
    if (-not [string]::IsNullOrWhiteSpace($Requested)) { return [IO.Path]::GetFullPath($Requested) }
    if (-not [string]::IsNullOrWhiteSpace($env:PHONELAB_DATA_ROOT)) {
        return [IO.Path]::GetFullPath($env:PHONELAB_DATA_ROOT)
    }
    $config = Get-PhoneLabLocalConfig
    if ($config.data_root) {
        $path = [string]$config.data_root
        if (-not [IO.Path]::IsPathRooted($path)) { $path = Join-Path $script:PhoneLabProjectRoot $path }
        return [IO.Path]::GetFullPath($path)
    }
    if (-not [string]::IsNullOrWhiteSpace($env:APPDATA)) {
        return [IO.Path]::GetFullPath((Join-Path $env:APPDATA 'PhoneLab\data'))
    }
    throw 'No PhoneLab data path is configured.'
}

function Resolve-PhoneLabPwsh {
    if (-not [string]::IsNullOrWhiteSpace($env:PHONELAB_PWSH)) { return [string]$env:PHONELAB_PWSH }
    $config = Get-PhoneLabLocalConfig
    if ($config.pwsh) {
        $path = [string]$config.pwsh
        if ([IO.Path]::IsPathRooted($path)) { return [IO.Path]::GetFullPath($path) }
        $command = Get-Command $path -ErrorAction SilentlyContinue
        if ($command) { return [string]$command.Source }
        return [IO.Path]::GetFullPath((Join-Path $script:PhoneLabProjectRoot $path))
    }
    $command = Get-Command 'pwsh.exe' -ErrorAction SilentlyContinue
    if (-not $command) { $command = Get-Command 'pwsh' -ErrorAction SilentlyContinue }
    if ($command) { return [string]$command.Source }
    throw 'PowerShell 7 was not found; set PHONELAB_PWSH or the local path configuration.'
}

function Resolve-PhoneLabPythonSource {
    if (-not [string]::IsNullOrWhiteSpace($env:PHONELAB_PYTHON_SOURCE)) { return [string]$env:PHONELAB_PYTHON_SOURCE }
    $config = Get-PhoneLabLocalConfig
    if ($config.python_source) {
        $path = [string]$config.python_source
        if (-not [IO.Path]::IsPathRooted($path)) { $path = Join-Path $script:PhoneLabProjectRoot $path }
        return [IO.Path]::GetFullPath($path)
    }
    $command = Get-Command 'python.exe' -ErrorAction SilentlyContinue
    if ($command) { return [string]$command.Source }
    throw 'Python source was not found; set PHONELAB_PYTHON_SOURCE or the local path configuration.'
}
