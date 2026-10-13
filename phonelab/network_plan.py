"""Reviewable firewall proposal only. Does not change firewall or start listeners."""
import ipaddress


def make_plan(address, prefix, interface, interfaces, uxplay, python):
    addr=ipaddress.ip_address(address)
    private=tuple(map(ipaddress.ip_network,('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')))
    if not isinstance(addr,ipaddress.IPv4Address) or not any(addr in n for n in private) or not 24 <= prefix <= 30:
        raise ValueError('explicit narrow RFC1918 interface required')
    subnet=ipaddress.ip_interface(f'{addr}/{prefix}').network
    if not interface or interface not in interfaces:raise ValueError('interface binding missing')
    outside=[str(n) for n in ipaddress.ip_network('0.0.0.0/0').address_exclude(subnet)]
    rules=[
        {'name':'UxPlay-Block-OutsideLAN-v4','Program':uxplay,'Action':'Block','Profile':'Any','Protocol':'Any','RemoteAddress':outside},
        {'name':'UxPlay-Block-v6','Program':uxplay,'Action':'Block','Profile':'Any','Protocol':'Any','RemoteAddress':['::/1','8000::/1']},
    ]
    others=[name for name in interfaces if name != interface]
    if others:rules.append({'name':'UxPlay-Block-OtherInterfaces','Program':uxplay,'Action':'Block','Profile':'Any',
                            'Protocol':'Any','InterfaceAlias':others,'RemoteAddress':['Any']})
    scope={'Profile':'Public','InterfaceAlias':interface,'RemoteAddress':[str(subnet)]}
    for protocol in ('TCP','UDP'):
        rules.append({'name':'UxPlay-Allow-'+protocol,'Program':uxplay,'Action':'Allow',**scope,
                      'Protocol':protocol,'LocalAddress':[address],'LocalPort':[53100,53101,53102]})
    rules.append({'name':'UxPlay-Allow-mDNS-v4','Program':uxplay,'Action':'Allow',**scope,
                  'Protocol':'UDP','LocalAddress':[address,'224.0.0.251'],'LocalPort':[5353]})
    rules.append({'name':'LabPage-Allow','Program':python,'Action':'Allow',**scope,
                  'Protocol':'TCP','LocalAddress':[address],'LocalPort':[8767]})
    # Existing broad allow rules must not expose unexpected receiver ports.
    rules.extend([
        {'name':'UxPlay-Block-OtherTCPPorts','Program':uxplay,'Action':'Block','Profile':'Any',
         'Protocol':'TCP','RemoteAddress':['Any'],'LocalPort':['0-53099','53103-65535']},
        {'name':'UxPlay-Block-OtherUDPPorts','Program':uxplay,'Action':'Block','Profile':'Any',
         'Protocol':'UDP','RemoteAddress':['Any'],'LocalPort':['0-5352','5354-53099','53103-65535']},
    ])
    for rule in rules:rule['Direction']='Inbound'
    # AirPlay replies stay on the approved LAN; IPv4 mDNS discovery is the sole
    # multicast destination exception. Never grant the receiver Internet egress.
    outbound=list(map(ipaddress.ip_network,outside))
    for exempt in map(ipaddress.ip_network,('224.0.0.251/32','127.0.0.1/32')):
        outbound=[part for network in outbound for part in
                  (network.address_exclude(exempt) if exempt.subnet_of(network) else [network])]
    outbound=list(map(str,outbound))
    rules.extend([
        {'name':'UxPlay-Block-OutsideLAN-v4-Out','Program':uxplay,'Action':'Block','Profile':'Any',
         'Direction':'Outbound','Protocol':'Any','RemoteAddress':outbound},
        {'name':'UxPlay-Block-v6-Out','Program':uxplay,'Action':'Block','Profile':'Any',
         'Direction':'Outbound','Protocol':'Any','RemoteAddress':['::/1','8000::/1']},
    ])
    if others:rules.append({'name':'UxPlay-Block-OtherInterfaces-Out','Program':uxplay,'Action':'Block','Profile':'Any',
                           'Direction':'Outbound','Protocol':'Any','InterfaceAlias':others,'RemoteAddress':['Any']})
    return {'execute_allowed':False,'group':'PhoneLab-esp32-iphone-bridge-20261013','rules':rules,
            'interface':interface,'local_address':address,'remote_subnet':str(subnet),
            'rollback':'Remove only these exact newly-created group/rule names. Never alter pre-existing rules.',
            'preflight':'Recheck current interface addresses, source/executable hashes, default/override/bypass rules and effective filters before any listener; block if confinement cannot be verified.',
            'reason':'UxPlay binds all interfaces. Explicit per-program block rules are needed in addition to scoped allows; other system services/settings remain outside this proposal.'}
