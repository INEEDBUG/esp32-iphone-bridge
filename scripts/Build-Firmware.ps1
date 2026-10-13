param(
    [switch]$SkipDependencyInstall,
    [string]$DataRoot = ''
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Paths.ps1')
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ToolRoot = Join-Path (Resolve-PhoneLabDataRoot $DataRoot) 'toolchain'
$CliPath = Join-Path $ToolRoot 'arduino-cli.exe'
$ArchivePath = Join-Path $ToolRoot 'arduino-cli_1.5.1_Windows_64bit.zip'
$ChecksumPath = Join-Path $ToolRoot 'arduino-cli_1.5.1-checksums.txt'
$ConfigPath = Join-Path $ToolRoot 'arduino-cli.yaml'
$CliVersion = '1.5.1'
$CliArchiveSha256 = 'fabe42e0eb04d00e776a66178299ff95a46c623dbc260f997e58fd514853dd40'
$Esp32CoreVersion = '3.3.11'
$Esp32CoreFile = Join-Path $ToolRoot 'data\packages\esp32\hardware\esp32\3.3.11\cores\esp32\esp32-hal-misc.c'
$CoreSourcePristineSha256 = '0f6057685866ce45208f1d1e5b9e81164aca1d580678b7be8c97227dacc970d9'
$CoreSourcePatchedSha256 = '7bb16eaf06d9198dc36b6d0fc01a900e608a3e08b60756f180fe385997fc232b'
$EspBleVersion = '1.3.0'
$ArduinoJsonVersion = '7.4.3'
$BoardFqbn = 'esp32:esp32:esp32'
$SketchPath = Join-Path $ProjectRoot 'firmware\classic_ble'
$LibraryPath = Join-Path $ProjectRoot 'firmware\libraries'
$BuildPath = Join-Path $ProjectRoot 'firmware\build\classic_ble'

New-Item -ItemType Directory -Force -Path $ToolRoot,(Join-Path $ToolRoot 'data'),(Join-Path $ToolRoot 'downloads'),(Join-Path $ToolRoot 'user') | Out-Null

if (-not (Test-Path -LiteralPath $CliPath)) {
    if (-not (Test-Path -LiteralPath $ArchivePath)) {
        Invoke-WebRequest -Uri 'https://github.com/arduino/arduino-cli/releases/download/v1.5.1/arduino-cli_1.5.1_Windows_64bit.zip' -OutFile $ArchivePath
    }
    $actualHash = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $CliArchiveSha256) {
        throw "Arduino CLI 1.5.1 archive checksum mismatch: $actualHash"
    }
    Expand-Archive -LiteralPath $ArchivePath -DestinationPath $ToolRoot -Force
}

$versionText = (& $CliPath version | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $versionText -notmatch "Version:\s+$([regex]::Escape($CliVersion))\b") {
    throw "Expected Arduino CLI $CliVersion, got: $versionText"
}

$dataDir = (Join-Path $ToolRoot 'data').Replace('\', '/')
$downloadsDir = (Join-Path $ToolRoot 'downloads').Replace('\', '/')
$userDir = (Join-Path $ToolRoot 'user').Replace('\', '/')
$indexUrl = 'https://espressif.github.io/arduino-esp32/package_esp32_index.json'
$config = @"
directories:
  data: "$dataDir"
  downloads: "$downloadsDir"
  user: "$userDir"
board_manager:
  additional_urls:
    - "$indexUrl"
sketch:
  always_export_binaries: false
"@
Set-Content -LiteralPath $ConfigPath -Value $config -Encoding utf8

$commonArgs = @('--config-file', $ConfigPath, '--no-color')
if (-not $SkipDependencyInstall) {
    & $CliPath @commonArgs core update-index
    if ($LASTEXITCODE -ne 0) { throw 'Arduino core index update failed.' }
    & $CliPath @commonArgs core install "esp32:esp32@$Esp32CoreVersion"
    if ($LASTEXITCODE -ne 0) { throw "ESP32 core $Esp32CoreVersion installation failed." }
    & $CliPath @commonArgs lib update-index
    if ($LASTEXITCODE -ne 0) { throw 'Arduino library index update failed.' }
    & $CliPath @commonArgs lib install "EspBle@$EspBleVersion" "ArduinoJson@$ArduinoJsonVersion"
    if ($LASTEXITCODE -ne 0) { throw 'Pinned Arduino library installation failed.' }
}

if (-not (Test-Path -LiteralPath $Esp32CoreFile)) {
    throw "Pinned ESP32 core source is missing: $Esp32CoreFile"
}
$coreHash = (Get-FileHash -LiteralPath $Esp32CoreFile -Algorithm SHA256).Hash.ToLowerInvariant()
if ($coreHash -eq $CoreSourcePristineSha256) {
    $coreSource = [IO.File]::ReadAllText($Esp32CoreFile)
    $oldRecovery = @'
  if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
    const esp_partition_t *partition = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_NVS, NULL);
    if (partition != NULL) {
      err = esp_partition_erase_range(partition, 0, partition->size);
      if (!err) {
        err = nvs_flash_init();
      } else {
        log_e("Failed to format the broken NVS partition!");
      }
    } else {
      log_e("Could not find NVS partition");
    }
  }
  if (err) {
    log_e("Failed to initialize NVS! Error: %d", err);
  }
'@
    $safeRecovery = @'
  if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
    log_e("NVS requires recovery; refusing to erase the NVS partition automatically.");
    while (true) {
      vTaskDelay(portMAX_DELAY);
    }
  }
  if (err) {
    log_e("Failed to initialize NVS! Error: %d", err);
    while (true) {
      vTaskDelay(portMAX_DELAY);
    }
  }
'@
    if (-not $coreSource.Contains($oldRecovery)) {
        throw 'Pinned Arduino-ESP32 source matched its digest but expected NVS recovery block was absent.'
    }
    $coreSource = $coreSource.Replace($oldRecovery, $safeRecovery)
    [IO.File]::WriteAllText($Esp32CoreFile, $coreSource, [Text.UTF8Encoding]::new($false))
    $coreHash = (Get-FileHash -LiteralPath $Esp32CoreFile -Algorithm SHA256).Hash.ToLowerInvariant()
}
if ($coreHash -ne $CoreSourcePatchedSha256) {
    throw "Pinned Arduino-ESP32 NVS source has an unexpected SHA-256 ($coreHash); refusing build."
}

New-Item -ItemType Directory -Force -Path $BuildPath | Out-Null
& $CliPath @commonArgs compile --fqbn $BoardFqbn --libraries $LibraryPath --output-dir $BuildPath $SketchPath
if ($LASTEXITCODE -ne 0) { throw 'Firmware compile failed.' }
Write-Output "Compile complete: $BuildPath"
