import time
import pytest

from phonelab.control import Controller, Frame, LabError


class RecordingBackend:
    name = 'unit-test-only'
    def __init__(self):
        self.calls = []
        self.fail = False
    def execute(self, request):
        self.calls.append(request)
        if self.fail:
            raise TimeoutError('lost ack')
        return {'state': 'executed'}
    def stop(self):
        return {'release_sent': True, 'release_confirmed': False}


def ready():
    backend = RecordingBackend()
    control = Controller('test-target', backend)
    control.acquire('tester')
    control.set_frame(Frame('test-target', 1, time.monotonic(), 390, 844, 'portrait', locked=False))
    control.calibrate(390, 844, 'portrait')
    return control, backend


def request(control, **updates):
    data = {'id': 'req1', 'session': control.session, 'owner': 'tester',
            'deadline': time.monotonic() + 2, 'op': 'move', 'args': {'dx': 10, 'dy': 20}}
    data.update(updates)
    return data


def test_absent_backend_never_reports_execution():
    c = Controller('target')
    assert c.get_status()['INPUT_AVAILABLE'] == 'UNKNOWN'
    with pytest.raises(LabError):
        c.submit({'op': 'click'})


@pytest.mark.parametrize('change', [
    {'op': 'type', 'args': {'text': 'hello\nclick'}},
    {'op': 'move', 'args': {'dx': True, 'dy': 0}},
    {'op': 'move', 'args': {'dx': 2049, 'dy': 0}},
    {'op': 'key', 'args': {'key': 'cmd+q'}},
    {'op': 'shell', 'args': {}},
    {'session': 'old-session'},
    {'owner': 'someone'},
    {'deadline': 0},
])
def test_invalid_request_does_not_touch_backend(change):
    c, b = ready()
    with pytest.raises(LabError):
        c.submit(request(c, **change))
    assert b.calls == []


def test_duplicate_click_is_not_replayed():
    c, b = ready()
    result = c.submit(request(c, op='click', args={}))
    assert result['state'] == 'executed'
    assert result['verified'] is False
    with pytest.raises(LabError):
        c.submit(request(c, op='click', args={}))
    assert len(b.calls) == 1


@pytest.mark.parametrize('mutation', ['stale', 'orientation', 'target', 'locked', 'generation'])
def test_observation_fault_disarms_input(mutation):
    c, b = ready()
    f = Frame('test-target', 2, time.monotonic(), 390, 844, 'portrait', locked=False)
    if mutation == 'stale': f.received_monotonic -= 10
    if mutation == 'orientation': f.orientation = 'landscape'
    if mutation == 'target': f.target = 'other-phone'
    if mutation == 'locked': f.locked = True
    if mutation == 'generation': c.disconnect('serial-lost')
    if mutation != 'generation': c.set_frame(f)
    with pytest.raises(LabError): c.submit(request(c))
    assert not b.calls


def test_timeout_result_unknown_no_retry_and_new_session():
    c, b = ready()
    b.fail = True
    old = c.session
    result = c.submit(request(c, op='click', args={}))
    assert result['state'] == 'unknown'
    assert c.session != old
    assert len(b.calls) == 1
    assert c.get_status()['armed'] is False


def test_stop_is_callable_without_owner_and_invalidates_session():
    c, b = ready()
    old = c.session
    result = c.stop()
    assert c.session != old
    assert result['release_confirmed'] is False
    with pytest.raises(LabError): c.submit(request(c))
    assert b.calls == []


def test_reconnect_does_not_reuse_old_calibration_or_owner():
    c, b = ready()
    c.disconnect('ble')
    c.acquire('new-owner')
    c.set_frame(Frame('test-target', 3, time.monotonic(), 390, 844, 'portrait', locked=False))
    with pytest.raises(LabError):
        c.submit(request(c, owner='new-owner'))
    assert not b.calls


def test_controller_lock_conflict():
    c, _ = ready()
    with pytest.raises(LabError): c.acquire('hermes')
