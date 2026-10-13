import threading
import time
import pytest
from test_control import ready, request
from test_serial_backend import Wire
from phonelab.serial_backend import SerialBackend
from phonelab.control import LabError


def test_stop_between_authorization_and_dispatch_never_sends_old_action():
    c, _ = ready()
    wire = Wire()
    backend = SerialBackend(wire)
    entered, resume = threading.Event(), threading.Event()
    execute = backend.execute
    def gated(r):
        entered.set()
        assert resume.wait(2)
        return execute(r)
    backend.execute = gated
    c.backend = backend
    r = request(c, op='click', args={})
    thread = threading.Thread(target=c.submit, args=(r,))
    thread.start()
    assert entered.wait(2)
    c.stop()
    resume.set()
    thread.join(2)
    assert not thread.is_alive()
    import json
    assert all(json.loads(raw)['op'] == 'stop' for raw in wire.writes)


def test_stale_submit_sends_stop_even_when_it_wins_watchdog_race():
    c, _ = ready()
    wire = Wire()
    c.backend = SerialBackend(wire)
    c.submit(request(c, op='press', args={}))
    c.frame.received_monotonic = time.monotonic()-10
    with pytest.raises(LabError):
        c.submit(request(c, id='stale'))
    import json
    assert json.loads(wire.writes[-1])['op'] == 'stop'
