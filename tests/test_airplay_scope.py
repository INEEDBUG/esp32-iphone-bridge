"""Exercise the actual PowerShell verifier with fictional, in-process firewall cmdlets."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from phonelab.network_plan import make_plan


@pytest.mark.parametrize('fault,expected',[
    ('none','AIRPLAY_SCOPE_VERIFIED'),
    ('unapproved','not approved'),
    ('wide','between /24 and /30'),
    ('different_network','does not contain'),
    ('missing','missing or ambiguous'),
    ('wrong_filter','filter differs'),
    ('noncontiguous_mask','non-contiguous IPv4 netmask'),
    ('unknown_prefix','unknown network prefix'),
    ('bypass','override block rules'),
    ('unknown_security','missing, unknown'),
    ('wrong_profile','Public network profile'),
    ('new_interface','network interfaces differ'),
])
def test_live_scope_verifier_with_mocked_windows_policy(tmp_path,fault,expected):
    pwsh=shutil.which('pwsh')
    if not pwsh:pytest.skip('PowerShell 7 is required for Windows verifier checks')
    data=tmp_path/'airplay-build'
    private=tmp_path/'private'
    data.mkdir();private.mkdir()
    uxplay=str(data/'uxplay-build/uxplay.exe')
    private_python=str(tmp_path/'toolchain/lan-python/python.exe')
    plan=make_plan('192.168.77.10',24,'Ethernet',['Ethernet','WiFi'],uxplay,private_python)
    plan['execute_allowed']=fault!='unapproved'
    if fault=='wide':plan['remote_subnet']='192.168.0.0/16'
    if fault=='different_network':plan['remote_subnet']='192.168.78.0/24'
    (private/'network-proposal.json').write_text(json.dumps(plan),encoding='utf-8')
    approval={'approved':True,'firewallScopeVerified':True,'programPath':uxplay,
              'interfaceAlias':plan['interface'],'localAddress':plan['local_address'],
              'remoteSubnet':plan['remote_subnet'],'firewallProfile':'Public',
              'frameOutputDirectory':str(private/'airplay-phone-frames'),
              'offlineValidationDirectory':str(private/'airplay-offline-validation')}
    (data/'receiver-run-approval.json').write_text(json.dumps(approval),encoding='utf-8')
    wrapper=tmp_path/'mock-policy.ps1'
    # The wrapper defines cmdlets only in its own child process; no real Windows
    # network/firewall cmdlet or listener is used by any case.
    wrapper.write_text(r'''
param($DataRoot,$Verifier,$Fault)
$p=Get-Content -LiteralPath (Join-Path (Split-Path $DataRoot) 'private/network-proposal.json') -Raw | ConvertFrom-Json
$mockRules=@($p.rules | ForEach-Object {
    $r=$_ | Select-Object *
    $r | Add-Member NoteProperty Name ($p.group+'-'+$_.name) -Force
    $r | Add-Member NoteProperty DisplayName $_.name -Force
    $r | Add-Member NoteProperty Group $p.group
    $r | Add-Member NoteProperty Enabled 'True'
    $r | Add-Member NoteProperty EdgeTraversalPolicy 'Block'
    $r
})
if($Fault -eq 'missing'){$mockRules=@($mockRules | Select-Object -Skip 1)}
if($Fault -eq 'wrong_filter'){$mockRules[0].RemoteAddress=@('Any')}
function Get-NetConnectionProfile {param($InterfaceAlias,$ErrorAction)
    $category=if($Fault -eq 'wrong_profile'){'Private'}else{'Public'}
    [pscustomobject]@{NetworkCategory=$category;InterfaceIndex=19}
}
function Get-NetIPAddress {param($InterfaceIndex,$AddressFamily,$ErrorAction)
    [pscustomobject]@{IPAddress='192.168.77.10';PrefixLength=24}
}
function Get-NetIPInterface {param([switch]$IncludeAllCompartments,$ErrorAction)
    'Ethernet','WiFi' | ForEach-Object {[pscustomobject]@{InterfaceAlias=$_;InterfaceIndex=19}}
    [pscustomobject]@{InterfaceAlias='Loopback';InterfaceIndex=1}
    if($Fault -eq 'new_interface'){[pscustomobject]@{InterfaceAlias='NewAdapter';InterfaceIndex=21}}
}
function Get-NetFirewallProfile {param($PolicyStore,$ErrorAction)
    'Domain','Private','Public' | ForEach-Object {
        [pscustomobject]@{Name=$_;Enabled='True';DefaultInboundAction='Block';AllowInboundRules='True';AllowLocalFirewallRules='True'}
    }
}
function Get-NetFirewallRule {param($PolicyStore,$Name,$DisplayName,$Enabled,$AssociatedNetFirewallSecurityFilter,$AssociatedNetFirewallApplicationFilter,$ErrorAction)
    if($Name){$mockRules | Where-Object Name -eq $Name}
    elseif($DisplayName){$mockRules | Where-Object DisplayName -eq $DisplayName}
    elseif($AssociatedNetFirewallSecurityFilter){if($AssociatedNetFirewallSecurityFilter.Marker -eq 'bypass'){[pscustomobject]@{Name='unrelated-bypass';Enabled='True'}}}
    elseif($AssociatedNetFirewallApplicationFilter){$mockRules | Where-Object Program -eq $AssociatedNetFirewallApplicationFilter.Program}
    else {
        $mockRules
    }
}
function Get-NetFirewallApplicationFilter {param($PolicyStore,$Program,$AssociatedNetFirewallRule,$ErrorAction)
    if($Program){[pscustomobject]@{Program=$Program}}
    else{[pscustomobject]@{Program=$AssociatedNetFirewallRule.Program}}
}
function Get-NetFirewallSecurityFilter {param($AssociatedNetFirewallRule,$PolicyStore,$OverrideBlockRules,$ErrorAction)
    if($AssociatedNetFirewallRule){
        if($Fault -eq 'unknown_security'){[pscustomobject]@{}}
        else{[pscustomobject]@{OverrideBlockRules='False'}}
    } elseif($Fault -eq 'bypass'){
        [pscustomobject]@{OverrideBlockRules='True';Marker='bypass'}
    }
}
function Get-NetFirewallAddressFilter {param($AssociatedNetFirewallRule,$ErrorAction)
    $r=$AssociatedNetFirewallRule
    $remote=@($r.RemoteAddress)
    if($r.DisplayName -eq 'UxPlay-Allow-TCP'){
        if($Fault -eq 'noncontiguous_mask'){$remote=@('192.168.77.0/255.0.255.0')}
        elseif($Fault -eq 'unknown_prefix'){$remote=@('192.168.77.0/33')}
        else{$remote=@('192.168.77.0/255.255.255.0')}
    } elseif($r.DisplayName -eq 'UxPlay-Block-OutsideLAN-v4-Out') {
        $remote=@($remote | ForEach-Object {if($_ -eq '127.0.0.0/32'){'127.0.0.0'}else{$_}})
    }
    [pscustomobject]@{RemoteAddress=$remote;LocalAddress=$(if($r.LocalAddress){$r.LocalAddress}else{'Any'})}
}
function Get-NetFirewallPortFilter {param($AssociatedNetFirewallRule,$ErrorAction)
    $r=$AssociatedNetFirewallRule
    [pscustomobject]@{Protocol=$r.Protocol;LocalPort=$(if($r.LocalPort){$r.LocalPort}else{'Any'});RemotePort='Any'}
}
function Get-NetFirewallInterfaceFilter {param($AssociatedNetFirewallRule,$ErrorAction)
    [pscustomobject]@{InterfaceAlias=$(if($AssociatedNetFirewallRule.InterfaceAlias){$AssociatedNetFirewallRule.InterfaceAlias}else{'Any'})}
}
& $Verifier -DataRoot $DataRoot
''',encoding='utf-8')
    verifier=Path(__file__).resolve().parents[1]/'scripts/Test-AirPlayScope.ps1'
    result=subprocess.run([pwsh,'-NoProfile','-File',str(wrapper),str(data),str(verifier),fault],
                          capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    assert expected in result.stdout,result.stdout+result.stderr
    if fault!='none':assert 'AIRPLAY_SCOPE_VERIFIED' not in result.stdout
