[CmdletBinding()]
param([switch]$RemoveRules)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Paths.ps1')
$Group = 'PhoneLab-esp32-iphone-bridge-20261013'
$TaskData = Resolve-PhoneLabDataRoot
$ProposalPath = Join-Path $TaskData 'private\network-proposal.json'
$ReceiptPath = Join-Path $TaskData 'private\network-apply-receipt.json'
$Proposal = Get-Content -LiteralPath $ProposalPath -Raw | ConvertFrom-Json
$Receipt = Get-Content -LiteralPath $ReceiptPath -Raw | ConvertFrom-Json
if ($Proposal.group -cne $Group -or $Receipt.group -cne $Group -or
    $Receipt.status -cne 'all_rules_created' -or @($Proposal.rules).Count -ne 12) {
    throw 'Rollback blocked: the successful task installation receipt is unavailable.'
}
$OwnedNames = @($Receipt.rules | Where-Object status -eq 'created' | ForEach-Object name)
if ($OwnedNames.Count -ne 12 -or @($OwnedNames | Sort-Object -Unique).Count -ne 12) {
    throw 'Rollback blocked: receipt does not identify exactly twelve owned rules.'
}
foreach ($Spec in $Proposal.rules) {
    if (($Group + '-' + $Spec.name) -cnotin $OwnedNames) { throw 'Rollback receipt/proposal mismatch.' }
}
if (-not $RemoveRules) {
    Write-Output 'PREVIEW_ONLY: stop the project AirPlay and LAN launchers first; no firewall query or mutation performed.'
    Write-Output 'Then run this script elevated with -RemoveRules to remove only the twelve receipted project rules.'
    return
}

# Removing confinement while an all-interface receiver is running would expose it.
$Programs = @($Proposal.rules | ForEach-Object Program | Sort-Object -Unique)
$ActiveProcesses = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
    $_.ExecutablePath -and [string]$_.ExecutablePath -iin $Programs
})
if ($ActiveProcesses.Count) { throw 'Rollback blocked: close the project AirPlay/LAN processes before removing confinement.' }

$Rules = @(Get-NetFirewallRule -PolicyStore PersistentStore -ErrorAction Stop | Where-Object Name -cin $OwnedNames)
foreach ($Rule in $Rules) {
    $Spec = @($Proposal.rules | Where-Object { ($Group + '-' + $_.name) -ceq $Rule.Name })
    $Application = Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $Rule -ErrorAction Stop
    if ($Spec.Count -ne 1 -or $Rule.Group -cne $Group -or
        -not [string]::Equals([IO.Path]::GetFullPath([string]$Application.Program),
                             [IO.Path]::GetFullPath([string]$Spec[0].Program),
                             [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Rollback blocked: an owned rule has drifted; no rules removed.'
    }
}
foreach ($Rule in $Rules) { Remove-NetFirewallRule -InputObject $Rule -ErrorAction Stop }
$Remaining = @(Get-NetFirewallRule -PolicyStore PersistentStore -ErrorAction Stop | Where-Object Name -cin $OwnedNames)
if ($Remaining.Count) { throw 'Project firewall rollback incomplete.' }
Write-Output ('PROJECT_FIREWALL_ROLLBACK_VERIFIED; removed=' + $Rules.Count)
