[CmdletBinding()]
param([switch]$Execute)
$ErrorActionPreference = 'Stop'
if ($Execute) { throw 'WRITE_LOCKED: separate explicit bounded firmware permission is required; this stage supports preview only.' }
$project = Split-Path -Parent $PSScriptRoot
$image = Join-Path $project 'firmware\build\classic_ble\classic_ble.ino.merged.bin'
if (-not (Test-Path -LiteralPath $image)) { throw 'Build the reviewed experimental image first.' }
$metadata = [ordered]@{
    State = 'PREVIEW_ONLY'
    Target = 'Only the physically confirmed CP2102 + classic ESP32 board; recheck live PnP/chip/security first'
    Address = '0x00000000'
    Length = (Get-Item -LiteralPath $image).Length
    Image = $image
    SHA256 = (Get-FileHash -LiteralPath $image -Algorithm SHA256).Hash
    Effect = 'Full experimental image replaces bootloader, partition table, app, OTA/NVS/config regions, including padding. OEM no longer remains in place.'
    Gate = 'Two independent OEM full reads must match, protected baseline verified, exact scope/iteration/recovery approved. No automatic write on USB arrival.'
    Rollback = 'Full original raw snapshot at address 0, target/security recheck and explicit restore permission; retain ROM state until startup separately justified. Restore has not been tested.'
    Radio = 'No intentional E01C GPIO/SPI/radio control; physical module power and OEM functions unverified.'
}
$metadata | ConvertTo-Json -Depth 4
