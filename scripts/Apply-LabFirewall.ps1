[CmdletBinding()]
param(
    [switch]$Apply,
    [string]$ProposalPath = '',
    [string]$ReceiptPath = ''
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Paths.ps1')
$TaskData = Resolve-PhoneLabDataRoot
if ([string]::IsNullOrWhiteSpace($ProposalPath)) { $ProposalPath = Join-Path $TaskData 'private\network-proposal.json' }
if ([string]::IsNullOrWhiteSpace($ReceiptPath)) { $ReceiptPath = Join-Path $TaskData 'private\network-apply-receipt.json' }
$ExpectedGroup = 'PhoneLab-esp32-iphone-bridge-20261013'
$ExpectedNames = @(
    'UxPlay-Block-OutsideLAN-v4', 'UxPlay-Block-v6', 'UxPlay-Block-OtherInterfaces',
    'UxPlay-Allow-TCP', 'UxPlay-Allow-UDP', 'UxPlay-Allow-mDNS-v4', 'LabPage-Allow',
    'UxPlay-Block-OutsideLAN-v4-Out', 'UxPlay-Block-v6-Out', 'UxPlay-Block-OtherInterfaces-Out',
    'UxPlay-Block-OtherTCPPorts', 'UxPlay-Block-OtherUDPPorts'
)
$ExpectedUxPlay = Join-Path $TaskData 'airplay-build\uxplay-build\uxplay.exe'
$ExpectedPython = Join-Path $TaskData 'toolchain\lan-python\python.exe'

function Assert-ValidAddress([string]$Value) {
    if ($Value -eq 'Any') { return }
    if ($Value.Contains('-')) {
        $ends=$Value.Split('-'); $first=$null; $last=$null
        if ($ends.Count -ne 2 -or -not [Net.IPAddress]::TryParse($ends[0],[ref]$first) -or
            -not [Net.IPAddress]::TryParse($ends[1],[ref]$last) -or $first.AddressFamily -ne $last.AddressFamily -or
            [String]::CompareOrdinal([Convert]::ToHexString($first.GetAddressBytes()),[Convert]::ToHexString($last.GetAddressBytes())) -gt 0) {
            throw 'Proposal contains an invalid address range.'
        }
        return
    }
    $parts = $Value.Split('/')
    if ($parts.Count -gt 2) { throw 'Proposal contains an invalid address.' }
    $parsed = $null
    if (-not [Net.IPAddress]::TryParse($parts[0], [ref]$parsed)) { throw 'Proposal contains an invalid address.' }
    if ($parts.Count -eq 2) {
        $prefix = 0
        if (-not [int]::TryParse($parts[1], [ref]$prefix) -or
            ($parsed.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork -and ($prefix -lt 0 -or $prefix -gt 32)) -or
            ($parsed.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetworkV6 -and ($prefix -lt 0 -or $prefix -gt 128))) {
            throw 'Proposal contains an invalid network prefix.'
        }
    }
}

function Get-ReviewedRemoteAddress([object]$Rule) {
    if ($Rule.name -in @('UxPlay-Block-v6','UxPlay-Block-v6-Out')) {
        $addresses = @($Rule.RemoteAddress)
        # The proposal uses the canonical disjoint halves of IPv6. For the
        # lower half, pass ::2/1: its leading bit is zero, so /1 normalizes
        # to ::/1 while avoiding an unspecified-address literal in parsing.
        if ($addresses.Count -ne 2 -or $addresses[0] -cne '::/1' -or $addresses[1] -cne '8000::/1') {
            throw 'IPv6 block proposal must contain exactly the reviewed canonical ::/1 and 8000::/1 halves.'
        }
        $low = [Net.IPAddress]::Parse('::2').GetAddressBytes()
        $high = [Net.IPAddress]::Parse('8000::').GetAddressBytes()
        if (($low[0] -band 128) -ne 0 -or $high[0] -ne 128 -or
            (@($high[1..15] | Where-Object { $_ -ne 0 }).Count -ne 0)) {
            throw 'IPv6 half-prefix mask verification failed.'
        }
        return @('::2/1','8000::/1')
    }
    return @($Rule.RemoteAddress)
}

function Assert-Rule([object]$Rule, [string[]]$AllowedInterfaces) {
    $allowedProperties = @('name','Program','Action','Profile','Protocol','RemoteAddress','Direction','InterfaceAlias','LocalAddress','LocalPort')
    foreach ($property in $Rule.PSObject.Properties.Name) {
        if ($property -cnotin $allowedProperties) { throw 'Proposal contains an unsupported rule property.' }
    }
    foreach ($required in @('name','Program','Action','Profile','Protocol','Direction')) {
        if (-not $Rule.PSObject.Properties[$required]) { throw 'Proposal is missing a required rule property.' }
    }
    if ($Rule.name -cnotin $ExpectedNames) { throw 'Proposal contains an unknown rule name.' }
    if (-not ([string]::Equals([string]$Rule.Program,$ExpectedUxPlay,[StringComparison]::OrdinalIgnoreCase) -or
              [string]::Equals([string]$Rule.Program,$ExpectedPython,[StringComparison]::OrdinalIgnoreCase))) {
        throw 'Proposal references an unexpected program path.'
    }
    $expectedProgram = if ($Rule.name -eq 'LabPage-Allow') { $ExpectedPython } else { $ExpectedUxPlay }
    if (-not [string]::Equals([string]$Rule.Program,$expectedProgram,[StringComparison]::OrdinalIgnoreCase)) {
        throw 'Proposal maps a rule to the wrong executable.'
    }
    $expectedAction = if ($Rule.name -like '*-Allow*') { 'Allow' } else { 'Block' }
    $expectedDirection = if ($Rule.name -match '-Out$') { 'Outbound' } else { 'Inbound' }
    if ($Rule.Action -cne $expectedAction -or $Rule.Direction -cne $expectedDirection) {
        throw 'Proposal action or direction does not match its reviewed rule name.'
    }
    if ($Rule.name -in @('UxPlay-Block-v6','UxPlay-Block-v6-Out')) { $null = Get-ReviewedRemoteAddress $Rule }
    if ($Rule.name -eq 'UxPlay-Block-OtherTCPPorts') {
        if ($Rule.Protocol -cne 'TCP' -or $Rule.Profile -cne 'Any' -or
            @($Rule.RemoteAddress).Count -ne 1 -or $Rule.RemoteAddress[0] -cne 'Any' -or
            (@($Rule.LocalPort) -join '|') -cne '0-53099|53103-65535') {
            throw 'TCP port-block rule does not match the reviewed complementary port ranges.'
        }
    }
    if ($Rule.name -eq 'UxPlay-Block-OtherUDPPorts') {
        if ($Rule.Protocol -cne 'UDP' -or $Rule.Profile -cne 'Any' -or
            @($Rule.RemoteAddress).Count -ne 1 -or $Rule.RemoteAddress[0] -cne 'Any' -or
            (@($Rule.LocalPort) -join '|') -cne '0-5352|5354-53099|53103-65535') {
            throw 'UDP port-block rule does not match the reviewed complementary port ranges.'
        }
    }
    if ($Rule.Action -cnotin @('Allow','Block') -or $Rule.Profile -cnotin @('Any','Public') -or
        $Rule.Protocol -cnotin @('Any','TCP','UDP') -or $Rule.Direction -cnotin @('Inbound','Outbound')) {
        throw 'Proposal contains an unsupported firewall value.'
    }
    foreach ($field in @('RemoteAddress','LocalAddress','InterfaceAlias','LocalPort')) {
        if ($Rule.PSObject.Properties[$field]) {
            $values = @($Rule.$field)
            if ($values.Count -eq 0) { throw 'Proposal contains an empty rule scope.' }
            foreach ($value in $values) {
                if ($null -eq $value -or [string]::IsNullOrWhiteSpace([string]$value)) { throw 'Proposal contains an empty rule scope.' }
                if ($field -eq 'RemoteAddress' -or $field -eq 'LocalAddress') { Assert-ValidAddress ([string]$value) }
                if ($field -eq 'InterfaceAlias' -and $value -cnotin $AllowedInterfaces) { throw 'Proposal contains an unreviewed interface.' }
                if ($field -eq 'LocalPort') {
                    if ($Rule.name -in @('UxPlay-Block-OtherTCPPorts','UxPlay-Block-OtherUDPPorts')) {
                        if ([string]$value -notmatch '^(?:0-53099|53103-65535|0-5352|5354-53099)$') {
                            throw 'Proposal contains an unreviewed port range.'
                        }
                    } else {
                        $port = 0
                        if ($value -is [bool] -or -not [int]::TryParse([string]$value,[ref]$port) -or $port -lt 1 -or $port -gt 65535) {
                            throw 'Proposal contains an invalid port.'
                        }
                    }
                }
            }
        }
    }
}

$proposal = Get-Content -LiteralPath $ProposalPath -Raw | ConvertFrom-Json
if ($proposal.group -cne $ExpectedGroup) { throw 'Proposal group does not match this task.' }
if ($proposal.rules -isnot [array] -or $proposal.rules.Count -ne 12) { throw 'Proposal must contain exactly twelve rules.' }
$names = @($proposal.rules | ForEach-Object { [string]$_.name })
if (($names | Sort-Object -Unique).Count -ne 12 -or (@($ExpectedNames | Where-Object { $_ -notin $names })).Count -ne 0) {
    throw 'Proposal rule names do not match the reviewed twelve-rule set.'
}
if ($proposal.interface -isnot [string] -or [string]::IsNullOrWhiteSpace($proposal.interface) -or
    $proposal.local_address -isnot [string] -or $proposal.remote_subnet -isnot [string]) {
    throw 'Proposal is missing its interface or address scope.'
}
$localAddress = $null
if (-not [Net.IPAddress]::TryParse($proposal.local_address,[ref]$localAddress) -or
    $localAddress.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) { throw 'Proposal local address must be IPv4.' }
$bytes = $localAddress.GetAddressBytes()
$isPrivate = ($bytes[0] -eq 10) -or ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) -or
    ($bytes[0] -eq 192 -and $bytes[1] -eq 168)
$networkParts = $proposal.remote_subnet.Split('/')
$networkAddress = $null
$prefix = 0
if (-not $isPrivate -or $networkParts.Count -ne 2 -or
    -not [Net.IPAddress]::TryParse($networkParts[0],[ref]$networkAddress) -or
    $networkAddress.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
    -not [int]::TryParse($networkParts[1],[ref]$prefix) -or $prefix -lt 24 -or $prefix -gt 30) {
    throw 'Proposal must use a narrow RFC1918 IPv4 network.'
}
$networkBytes = $networkAddress.GetAddressBytes()
$localNumber = ([uint64]$bytes[0] * 16777216) + ([uint64]$bytes[1] * 65536) + ([uint64]$bytes[2] * 256) + $bytes[3]
$networkNumber = ([uint64]$networkBytes[0] * 16777216) + ([uint64]$networkBytes[1] * 65536) + ([uint64]$networkBytes[2] * 256) + $networkBytes[3]
$mask = [uint64]([math]::Pow(2,32) - [math]::Pow(2,32-$prefix))
if (($localNumber -band $mask) -ne ($networkNumber -band $mask)) { throw 'Proposal local address is outside its remote subnet.' }
$allowedInterfaces = @($proposal.interface)
foreach ($rule in $proposal.rules) {
    if ($rule.PSObject.Properties['InterfaceAlias']) { $allowedInterfaces += @($rule.InterfaceAlias) }
}
$allowedInterfaces = @($allowedInterfaces | Sort-Object -Unique)
foreach ($rule in $proposal.rules) { Assert-Rule $rule $allowedInterfaces }

if (-not $Apply) {
    Write-Output 'PREVIEW ONLY: no firewall query or mutation was performed.'
    Write-Output ("Validated proposal: group={0}; rules={1}; programs=2; addresses and interface scope are private." -f $proposal.group,$proposal.rules.Count)
    Write-Output 'To apply, use -Apply from an elevated PowerShell only after the approved proposal sets execute_allowed=true.'
    return
}
if ($proposal.execute_allowed -ne $true) { throw 'Apply blocked: proposal execute_allowed must be true.' }

Import-Module NetSecurity -ErrorAction Stop
$fullNames = @($ExpectedNames | ForEach-Object { "$ExpectedGroup-$_" })
$expectedDisplayNames = @($ExpectedNames) + @($fullNames)
$existingRules = @(Get-NetFirewallRule -ErrorAction Stop)
$collisions = @($existingRules | Where-Object { $_.Name -cin $fullNames -or $_.DisplayName -cin $expectedDisplayNames -or $_.Group -ceq $ExpectedGroup })
if ($collisions.Count -gt 0) { throw 'Apply blocked: an expected rule name or task group already exists; no rules were changed.' }

$created = [System.Collections.Generic.List[object]]::new()
$results = [System.Collections.Generic.List[object]]::new()
$failure = $null
$failureDetails = $null
try {
    # Install every block rule before any allows.
    $applyRules = @($proposal.rules | Where-Object { $_.Action -eq 'Block' }) +
        @($proposal.rules | Where-Object { $_.Action -eq 'Allow' })
    foreach ($rule in $applyRules) {
        $fullName = "$ExpectedGroup-$($rule.name)"
        $params = @{
            Name = $fullName
            DisplayName = [string]$rule.name
            Group = $ExpectedGroup
            Program = [string]$rule.Program
            Action = [string]$rule.Action
            Profile = @($rule.Profile)
            Protocol = [string]$rule.Protocol
            Direction = [string]$rule.Direction
            Enabled = 'True'
            EdgeTraversalPolicy = 'Block'
        }
        foreach ($field in @('RemoteAddress','InterfaceAlias','LocalAddress','LocalPort')) {
            if ($rule.PSObject.Properties[$field]) {
                if ($field -eq 'RemoteAddress') { $params[$field] = Get-ReviewedRemoteAddress $rule }
                else { $params[$field] = @($rule.$field) }
            }
        }
        $newRule = New-NetFirewallRule @params -ErrorAction Stop
        $created.Add($newRule)
        $results.Add([pscustomobject]@{ name=$newRule.Name; status='created' })
    }
} catch {
    $caughtError = $_
    $failureDetails = @{ type=$caughtError.Exception.GetType().FullName; id=$caughtError.FullyQualifiedErrorId;
        message=$caughtError.Exception.Message; script_line=$caughtError.InvocationInfo.ScriptLineNumber }
    $failure = 'apply_failed; only rules created by this invocation were rolled back'
    for ($index = $created.Count - 1; $index -ge 0; $index--) {
        $createdRule = $created[$index]
        try {
            Remove-NetFirewallRule -InputObject $createdRule -ErrorAction Stop
            $results.Add([pscustomobject]@{ name=$createdRule.Name; status='rolled_back' })
        } catch {
            $results.Add([pscustomobject]@{ name=$createdRule.Name; status='rollback_failed' })
            $failure = 'apply_failed_and_rollback_incomplete'
        }
    }
}

$receipt = [pscustomobject]@{
    group = $ExpectedGroup
    status = if ($failure) { $failure } else { 'all_rules_created' }
    failure_details = $failureDetails
    rules = @($results.ToArray())
    utc = [DateTime]::UtcNow.ToString('o')
}
$receiptDirectory = Split-Path -Parent $ReceiptPath
if (-not (Test-Path -LiteralPath $receiptDirectory -PathType Container)) { New-Item -ItemType Directory -Path $receiptDirectory -Force | Out-Null }
$receipt | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $ReceiptPath -Encoding UTF8
if ($failure) { throw $failure }
Write-Output ("Firewall proposal applied: {0} rules; receipt={1}" -f $created.Count,$ReceiptPath)
