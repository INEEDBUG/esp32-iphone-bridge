import ipaddress
import pytest
from phonelab.network_plan import make_plan


def test_network_plan_only_targets_project_executables_and_blocks_other_networks():
    p=make_plan('192.168.77.10',24,'Ethernet',['Ethernet','WiFi'],'uxplay.exe','python.exe')
    block=[r for r in p['rules'] if r['name']=='UxPlay-Block-OutsideLAN-v4'][0]
    nets=[ipaddress.ip_network(x) for x in block['RemoteAddress']]
    assert not any(ipaddress.ip_address('192.168.77.25') in n for n in nets)
    assert any(ipaddress.ip_address('8.8.8.8') in n for n in nets)
    assert any(ipaddress.ip_address('192.168.78.10') in n for n in nets)
    assert p['execute_allowed'] is False
    assert all(r['Program'] in ('uxplay.exe','python.exe') for r in p['rules'])
    assert all(r['InterfaceAlias']=='Ethernet' for r in p['rules'] if r['Action']=='Allow')


@pytest.mark.parametrize('address,prefix',[('0.0.0.0',24),('127.0.0.1',24),('8.8.8.8',24),('192.168.0.2',16)])
def test_refuse_unbounded_or_nonprivate_scope(address,prefix):
    with pytest.raises(ValueError): make_plan(address,prefix,'Ethernet',['Ethernet'],'u','p')


def test_outbound_uxplay_destinations_exclude_public_other_lan_and_ipv6():
    p=make_plan('192.168.77.10',24,'Ethernet',['Ethernet','WiFi'],'uxplay.exe','python.exe')
    rules=[r for r in p['rules'] if r['Direction']=='Outbound' and r['Action']=='Block']
    scopes=[n for r in rules for n in r.get('RemoteAddress',[]) if n!='Any']
    nets=[ipaddress.ip_network(n) for n in scopes if '-' not in n]
    ranges=[tuple(map(ipaddress.ip_address,n.split('-'))) for n in scopes if '-' in n]
    def blocked(address):
        a=ipaddress.ip_address(address)
        return any(a in n for n in nets) or any(a.version==lo.version and lo<=a<=hi for lo,hi in ranges)
    for address in ('8.8.8.8','192.168.78.10','2001:db8::1'):
        assert blocked(address)
    for address in ('192.168.77.25','224.0.0.251','127.0.0.1'):
        assert not blocked(address)
    assert blocked('127.0.0.2')


def test_port_blocks_cover_every_other_port_for_broad_existing_allows():
    plan=make_plan('192.168.77.10',24,'Ethernet',['Ethernet','WiFi'],'u','p')
    for protocol,allowed in [('TCP',{53100,53101,53102}),('UDP',{5353,53100,53101,53102})]:
        row=next(r for r in plan['rules'] if r['name']=='UxPlay-Block-Other'+protocol+'Ports')
        intervals=[tuple(map(int,value.split('-'))) for value in row['LocalPort']]
        for port in range(65536):
            assert any(lo<=port<=hi for lo,hi in intervals)==(port not in allowed)
        assert row['Action']=='Block' and row['RemoteAddress']==['Any']
