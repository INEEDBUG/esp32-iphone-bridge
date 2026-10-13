[CmdletBinding()]
param(
    [string]$DataRoot = ''
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Paths.ps1')
if ([string]::IsNullOrWhiteSpace($DataRoot)) { $DataRoot = Join-Path (Resolve-PhoneLabDataRoot) 'airplay-build' }

function Stop-ScopeCheck([string]$Reason) {
    throw $Reason
}

function Get-FieldValue($Object, [string]$Name, $Default = 'Any') {
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value -or @($property.Value).Count -eq 0) {
        return $Default
    }
    return $property.Value
}

function Normalize-Set($Value, [string]$Field) {
    $values = @()
    foreach ($item in @($Value)) {
        if ($null -eq $item) { continue }
        foreach ($part in ([string]$item -split '[,;]')) {
            $normalized = $part.Trim().Trim('{}')
            if ($Field -in @('RemoteAddress','LocalAddress')) {
                if ($normalized.Contains('/')) {
                    $cidr=$normalized.Split('/'); $ip=$null; $prefix=0
                    if ($cidr.Count -ne 2 -or -not [Net.IPAddress]::TryParse($cidr[0],[ref]$ip)) {
                        Stop-ScopeCheck 'an address filter contains an invalid address or prefix'
                    }
                    $bytes=$ip.GetAddressBytes()
                    if ([int]::TryParse($cidr[1],[ref]$prefix)) {
                        if ($prefix -lt 0 -or $prefix -gt $bytes.Count*8) {
                            Stop-ScopeCheck 'an address filter contains an unknown network prefix'
                        }
                    } else {
                        $netmask=$null
                        if ($ip.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
                            -not [Net.IPAddress]::TryParse($cidr[1],[ref]$netmask) -or
                            $netmask.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
                            Stop-ScopeCheck 'an address filter contains an unknown network prefix'
                        }
                        $prefix=0; $sawZero=$false
                        foreach ($maskByte in $netmask.GetAddressBytes()) {
                            for ($bit=7; $bit -ge 0; $bit--) {
                                if (($maskByte -band (1 -shl $bit)) -ne 0) {
                                    if ($sawZero) { Stop-ScopeCheck 'an address filter contains a non-contiguous IPv4 netmask' }
                                    $prefix++
                                } else { $sawZero=$true }
                            }
                        }
                    }
                    for($i=0;$i -lt $bytes.Count;$i++) {
                        $bits=$prefix-$i*8
                        $mask=if($bits -ge 8){255}elseif($bits -le 0){0}else{(255 -shl (8-$bits)) -band 255}
                        $bytes[$i]=$bytes[$i] -band $mask
                    }
                    $normalized=[Net.IPAddress]::new($bytes).ToString()+'/'+$prefix
                } else {
                    $hostAddress=$null
                    if ([Net.IPAddress]::TryParse($normalized,[ref]$hostAddress)) {
                        $hostPrefix=if($hostAddress.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork){32}else{128}
                        $normalized=$hostAddress.ToString()+'/'+$hostPrefix
                    }
                }
            }
            if ($normalized) { $values += $normalized }
        }
    }
    if (-not $values.Count) { $values = @('Any') }
    if ($Field -eq 'RemoteAddress' -and (($values.Count -eq 1 -and $values[0] -eq '::/0') -or
        ($values.Count -eq 2 -and '::/1' -in $values -and '8000::/1' -in $values))) { $values=@('Any6') }
    if ($Field -eq 'Protocol') {
        $values = @($values | ForEach-Object {
            switch -Regex ($_.ToLowerInvariant()) {
                '^(6|tcp)$' { 'TCP'; break }
                '^(17|udp)$' { 'UDP'; break }
                '^(256|any)$' { 'Any'; break }
                default { $_ }
            }
        })
    } elseif ($Field -eq 'Program') {
        $values = @($values | ForEach-Object {
            if ($_ -eq 'Any') { 'Any' }
            else { [IO.Path]::GetFullPath($_.Replace('/', '\')).TrimEnd('\').ToLowerInvariant() }
        })
    } else {
        $values = @($values | ForEach-Object { $_.ToLowerInvariant() })
    }
    return (@($values | Sort-Object -Unique) -join '|')
}

function Convert-IPv4Bytes([string]$Address) {
    $parsed = $null
    if (-not [Net.IPAddress]::TryParse($Address, [ref]$parsed) -or
        $parsed.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
        Stop-ScopeCheck 'proposal or live network contains an invalid IPv4 address'
    }
    return ,$parsed.GetAddressBytes()
}

try {
    $DataRoot = [IO.Path]::GetFullPath($DataRoot)
    $TaskRoot = Split-Path $DataRoot -Parent
    $PrivateRoot = Join-Path $TaskRoot 'private'
    $ProposalPath = Join-Path $PrivateRoot 'network-proposal.json'
    $ApprovalPath = Join-Path $DataRoot 'receiver-run-approval.json'
    $UxPlayPath = Join-Path $DataRoot 'uxplay-build\uxplay.exe'
    $FramesPath = Join-Path $PrivateRoot 'airplay-phone-frames'
    $OfflinePath = Join-Path $PrivateRoot 'airplay-offline-validation'
    $LanPythonPath = Join-Path $TaskRoot 'toolchain\lan-python\python.exe'

    if (-not (Test-Path -LiteralPath $ProposalPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $ApprovalPath -PathType Leaf)) {
        Stop-ScopeCheck 'network proposal or approval marker is missing'
    }
    $proposal = Get-Content -LiteralPath $ProposalPath -Raw | ConvertFrom-Json
    $approval = Get-Content -LiteralPath $ApprovalPath -Raw | ConvertFrom-Json
    if ($proposal.execute_allowed -ne $true) { Stop-ScopeCheck 'network proposal is not approved for execution' }
    # This read-only verifier establishes the flag; the launcher separately
    # requires the persisted flag as well as a fresh successful verification.
    if ($approval.approved -ne $true) {
        Stop-ScopeCheck 'approval marker is not enabled'
    }
    if (@($proposal.rules).Count -ne 12) { Stop-ScopeCheck 'proposal does not contain the reviewed twelve-rule set' }
    $expectedRuleNames = @(
        'UxPlay-Block-OutsideLAN-v4', 'UxPlay-Block-v6', 'UxPlay-Block-OtherInterfaces',
        'UxPlay-Allow-TCP', 'UxPlay-Allow-UDP', 'UxPlay-Allow-mDNS-v4', 'LabPage-Allow',
        'UxPlay-Block-OutsideLAN-v4-Out', 'UxPlay-Block-v6-Out', 'UxPlay-Block-OtherInterfaces-Out',
        'UxPlay-Block-OtherTCPPorts', 'UxPlay-Block-OtherUDPPorts'
    )
    $proposalNames = @($proposal.rules | ForEach-Object { [string]$_.name } | Sort-Object)
    if (($proposalNames -join '|') -ne (@($expectedRuleNames | Sort-Object) -join '|') -or
        @($proposalNames | Select-Object -Unique).Count -ne 12) {
        Stop-ScopeCheck 'proposal rule names do not match the reviewed set'
    }

    $expectedPortBlocks = @(
        @{ Name='UxPlay-Block-OtherTCPPorts'; Protocol='TCP'; Ports=@('0-53099','53103-65535') },
        @{ Name='UxPlay-Block-OtherUDPPorts'; Protocol='UDP'; Ports=@('0-5352','5354-53099','53103-65535') }
    )
    foreach ($spec in $expectedPortBlocks) {
        $portRule = @($proposal.rules | Where-Object name -eq $spec.Name)
        if ($portRule.Count -ne 1 -or
            (Normalize-Set (Get-FieldValue $portRule[0] 'Program' 'Any') 'Program') -ne (Normalize-Set $UxPlayPath 'Program') -or
            [string]$portRule[0].Action -ne 'Block' -or [string]$portRule[0].Direction -ne 'Inbound' -or
            [string]$portRule[0].Profile -ne 'Any' -or [string]$portRule[0].Protocol -ne $spec.Protocol -or
            (Normalize-Set (Get-FieldValue $portRule[0] 'RemoteAddress' 'Any') 'RemoteAddress') -ne 'any' -or
            (Normalize-Set (Get-FieldValue $portRule[0] 'LocalAddress' 'Any') 'LocalAddress') -ne 'any' -or
            (Normalize-Set (Get-FieldValue $portRule[0] 'LocalPort' 'Any') 'LocalPort') -ne (Normalize-Set $spec.Ports 'LocalPort')) {
            Stop-ScopeCheck 'proposal TCP/UDP complement block rule differs from the exact reviewed port ranges'
        }
    }
    $labPageRule = @($proposal.rules | Where-Object name -eq 'LabPage-Allow')
    if ($labPageRule.Count -ne 1 -or
        (Normalize-Set (Get-FieldValue $labPageRule[0] 'Program' 'Any') 'Program') -ne (Normalize-Set $LanPythonPath 'Program')) {
        Stop-ScopeCheck 'lab-page allow rule does not target the task-private Python runtime'
    }

    $prefixMatch = [regex]::Match([string]$proposal.remote_subnet, '^([^/]+)/([0-9]{1,2})$')
    if (-not $prefixMatch.Success) { Stop-ScopeCheck 'proposal subnet is not IPv4 CIDR' }
    $prefixLength = [int]$prefixMatch.Groups[2].Value
    if ($prefixLength -lt 24 -or $prefixLength -gt 30) {
        Stop-ScopeCheck 'proposal subnet prefix must be between /24 and /30'
    }
    $networkBytes = Convert-IPv4Bytes $prefixMatch.Groups[1].Value
    $localBytes = Convert-IPv4Bytes ([string]$proposal.local_address)
    $isRfc1918 = ($networkBytes[0] -eq 10) -or
        ($networkBytes[0] -eq 172 -and $networkBytes[1] -ge 16 -and $networkBytes[1] -le 31) -or
        ($networkBytes[0] -eq 192 -and $networkBytes[1] -eq 168)
    if (-not $isRfc1918) { Stop-ScopeCheck 'proposal subnet is outside RFC1918 space' }
    for ($i = 0; $i -lt 4; $i++) {
        $bits = $prefixLength - ($i * 8)
        if ($bits -ge 8) { $mask = 255 }
        elseif ($bits -le 0) { $mask = 0 }
        else { $mask = (255 -shl (8 - $bits)) -band 255 }
        if (($networkBytes[$i] -band $mask) -ne $networkBytes[$i]) {
            Stop-ScopeCheck 'proposal subnet address is not the canonical network address'
        }
        if (($localBytes[$i] -band $mask) -ne $networkBytes[$i]) {
            Stop-ScopeCheck 'proposal subnet does not contain its local address'
        }
    }

    $connections = @(Get-NetConnectionProfile -InterfaceAlias ([string]$proposal.interface) -ErrorAction Stop)
    if ($connections.Count -ne 1 -or [string]$connections[0].NetworkCategory -ne 'Public') {
        Stop-ScopeCheck 'intended interface does not have exactly one live Public network profile'
    }
    $interfaceIndex = [int]$connections[0].InterfaceIndex
    $liveAddresses = @(Get-NetIPAddress -InterfaceIndex $interfaceIndex -AddressFamily IPv4 -ErrorAction Stop |
        Where-Object { [string]$_.IPAddress -eq [string]$proposal.local_address })
    if ($liveAddresses.Count -ne 1 -or [int]$liveAddresses[0].PrefixLength -ne $prefixLength) {
        Stop-ScopeCheck 'live interface IPv4 address or prefix differs from the proposal'
    }
    # NetSecurity resolves aliases from the IP stack. Not-present NDIS adapter
    # names are not valid firewall interfaces. Index 1 is host loopback, which
    # carries the receiver's private RTP capture and is not a LAN interface.
    $liveOthers = @(Get-NetIPInterface -IncludeAllCompartments -ErrorAction Stop |
        Where-Object { $_.InterfaceIndex -ne 1 -and $_.InterfaceAlias -ne [string]$proposal.interface } |
        ForEach-Object { [string]$_.InterfaceAlias } | Sort-Object -Unique)
    $otherInterfaceBlock = @($proposal.rules | Where-Object name -eq 'UxPlay-Block-OtherInterfaces')[0]
    if ((Normalize-Set $liveOthers 'InterfaceAlias') -ne
        (Normalize-Set $otherInterfaceBlock.InterfaceAlias 'InterfaceAlias')) {
        Stop-ScopeCheck 'live network interfaces differ from the reviewed proposal'
    }

    $firewallProfiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop)
    foreach ($name in @('Domain', 'Private', 'Public')) {
        $profile = @($firewallProfiles | Where-Object { [string]$_.Name -eq $name })
        if ($profile.Count -ne 1 -or [string]$profile[0].Enabled -ne 'True' -or
            [string]$profile[0].DefaultInboundAction -ne 'Block' -or
            [string]$profile[0].AllowInboundRules -ne 'True' -or
            [string]$profile[0].AllowLocalFirewallRules -ne 'True') {
            Stop-ScopeCheck "ActiveStore firewall profile $name is not enabled with blocking inbound policy and effective inbound rules"
        }
    }

    if ([IO.Path]::GetFullPath([string]$approval.programPath) -ne [IO.Path]::GetFullPath($UxPlayPath) -or
        [string]$approval.interfaceAlias -ne [string]$proposal.interface -or
        [string]$approval.localAddress -ne [string]$proposal.local_address -or
        [string]$approval.remoteSubnet -ne [string]$proposal.remote_subnet -or
        [string]$approval.firewallProfile -ne 'Public' -or
        [IO.Path]::GetFullPath([string]$approval.frameOutputDirectory) -ne [IO.Path]::GetFullPath($FramesPath) -or
        [IO.Path]::GetFullPath([string]$approval.offlineValidationDirectory) -ne [IO.Path]::GetFullPath($OfflinePath)) {
        Stop-ScopeCheck 'approval marker does not match the live proposal or task-private output paths'
    }

    $installedNames = @{}
    foreach ($expected in $proposal.rules) {
        $installedName = [string]$proposal.group + '-' + [string]$expected.name
        $expectedProgram = if ($expected.name -eq 'LabPage-Allow') { $LanPythonPath } else { $UxPlayPath }
        if ((Normalize-Set (Get-FieldValue $expected 'Program' 'Any') 'Program') -ne
            (Normalize-Set $expectedProgram 'Program')) {
            Stop-ScopeCheck 'a UxPlay proposal rule does not target the pinned executable'
        }
        $matches = @(Get-NetFirewallRule -PolicyStore ActiveStore -Name $installedName -ErrorAction Stop)
        if ($matches.Count -ne 1) { Stop-ScopeCheck 'an expected ActiveStore rule is missing or ambiguous' }
        $rule = $matches[0]
        $installedNames[[string]$rule.Name] = $true
        if ([string]$rule.Group -ne [string]$proposal.group -or [string]$rule.Enabled -ne 'True' -or
            [string]$rule.EdgeTraversalPolicy -ne 'Block') {
            Stop-ScopeCheck 'an ActiveStore rule has an unexpected group, state, edge traversal, or block override'
        }

        $securityFilters = @(Get-NetFirewallSecurityFilter -AssociatedNetFirewallRule $rule -PolicyStore ActiveStore -ErrorAction Stop)
        if ($securityFilters.Count -ne 1 -or
            -not $securityFilters[0].PSObject.Properties['OverrideBlockRules'] -or
            [string]$securityFilters[0].OverrideBlockRules -cne 'False') {
            Stop-ScopeCheck 'an ActiveStore rule has a missing, unknown, or enabled block-override security filter'
        }

        $app = Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $rule -ErrorAction Stop
        $address = Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $rule -ErrorAction Stop
        $port = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $rule -ErrorAction Stop
        $interface = Get-NetFirewallInterfaceFilter -AssociatedNetFirewallRule $rule -ErrorAction Stop
        $actual = @{
            Program = $app.Program
            Action = $rule.Action
            Profile = $rule.Profile
            Protocol = $port.Protocol
            RemoteAddress = $address.RemoteAddress
            Direction = $rule.Direction
            InterfaceAlias = $interface.InterfaceAlias
            LocalAddress = $address.LocalAddress
            LocalPort = $port.LocalPort
            RemotePort = $port.RemotePort
        }
        foreach ($field in $actual.Keys) {
            $expectedValue = Get-FieldValue $expected $field 'Any'
            $actualValue = $actual[$field]
            if ((Normalize-Set $expectedValue $field) -ne (Normalize-Set $actualValue $field)) {
                Stop-ScopeCheck "an ActiveStore rule filter differs from the proposal ($field)"
            }
        }
    }

    # An unmatched CIM property query throws instead of returning an empty set.
    # Read the store once so absence of override rules remains a verified result.
    $overrideFilters = @(Get-NetFirewallSecurityFilter -PolicyStore ActiveStore -ErrorAction Stop |
        Where-Object { [string]$_.OverrideBlockRules -eq 'True' })
    foreach ($securityFilter in $overrideFilters) {
        $overrideRules = @(Get-NetFirewallRule -AssociatedNetFirewallSecurityFilter $securityFilter -PolicyStore ActiveStore -ErrorAction Stop |
            Where-Object { [string]$_.Enabled -eq 'True' })
        foreach ($candidate in $overrideRules) {
            Stop-ScopeCheck 'an enabled ActiveStore rule can override block rules'
        }
    }

    $uxplayApplicationFilters = @(Get-NetFirewallApplicationFilter -PolicyStore ActiveStore -Program $UxPlayPath -ErrorAction Stop)
    foreach ($applicationFilter in $uxplayApplicationFilters) {
        $uxplayRules = @(Get-NetFirewallRule -AssociatedNetFirewallApplicationFilter $applicationFilter -PolicyStore ActiveStore -ErrorAction Stop |
            Where-Object { [string]$_.Enabled -eq 'True' })
        foreach ($candidate in $uxplayRules) {
            if (-not $installedNames.ContainsKey([string]$candidate.Name)) {
                Stop-ScopeCheck 'an additional enabled ActiveStore rule targets the UxPlay executable'
            }
        }
    }

    return 'AIRPLAY_SCOPE_VERIFIED'
} catch {
    return ('AIRPLAY_SCOPE_BLOCKED: ' + $_.Exception.Message)
}
