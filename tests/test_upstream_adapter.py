import asyncio
import pytest
from test_control import ready
from phonelab.upstream_adapter import GuardedDevice, AdapterError


def test_original_pointer_home_maps_to_pointer_home():
    c, b = ready()
    device = GuardedDevice(c, 'tester')
    asyncio.run(device.send('home'))
    assert b.calls[0]['op'] == 'home'
    assert b.calls[0]['args'] == {}


@pytest.mark.parametrize('line', ['type test123\nclick', 'reboot', 'move nan 10', 'key cmd+h'])
def test_upstream_boundary_rejects_unsupported_or_injected_command(line):
    c, b = ready()
    with pytest.raises(AdapterError): asyncio.run(GuardedDevice(c,'tester').send(line))
    assert not b.calls


def test_unknown_result_is_not_converted_to_ok():
    c, b = ready()
    b.fail = True
    with pytest.raises(AdapterError): asyncio.run(GuardedDevice(c,'tester').send('click'))
    assert len(b.calls) == 1


def test_reconnect_bumps_upstream_links_and_requires_new_owner():
    c, b = ready()
    d = GuardedDevice(c,'tester')
    old = d.links
    c.stop()
    assert d.links == old+1
    assert d.connected is False
