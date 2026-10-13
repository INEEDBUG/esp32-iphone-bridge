import json
import time
import pytest
from phonelab.serial_backend import SerialBackend


class Wire:
    def __init__(self, wrong_id=False, mouse_ready=True):
        self.lines = []
        self.writes = []
        self.wrong_id = wrong_id
        self.mouse_ready = mouse_ready
    def write(self, raw):
        self.writes.append(raw)
        r = json.loads(raw)
        self.lines.append(json.dumps({'id': 'wrong' if self.wrong_id else r['id'],
            'session': r['session'], 'state': 'executed', 'ble_connected': True,
            'mouse_ready': self.mouse_ready}).encode() + b'\n')
    def readline(self, size=513): return self.lines.pop(0) if self.lines else b''
    def close(self): pass


def test_serial_ack_matches_generation_and_id_and_never_retries():
    wire = Wire()
    b = SerialBackend(wire)
    r = {'id': 'a', 'session': 's', 'op': 'click', 'args': {}, 'deadline': time.monotonic()+.1}
    assert b.execute(r)['state'] == 'executed'
    assert len(wire.writes) == 2  # handshake plus exactly one click
    assert json.loads(wire.writes[0])['op'] == 'hello'


def test_wrong_ack_unknown_and_no_click_retry():
    wire = Wire(wrong_id=True)
    b = SerialBackend(wire)
    with pytest.raises(TimeoutError):
        b.execute({'id':'a','session':'s','op':'click','args':{},'deadline':time.monotonic()+.02})
    assert len(wire.writes) == 1  # failed hello prevents action


def test_stop_reports_release_unconfirmed():
    b = SerialBackend(Wire())
    result = b.stop()
    assert result['release_confirmed'] is False


def test_long_action_gets_heartbeat_without_replaying_click():
    class DelayedWire(Wire):
        def write(self, raw):
            r = json.loads(raw)
            if r['op'] == 'click':
                self.writes.append(raw)
                self.pending = r
                self.started = time.monotonic()
            else:
                super().write(raw)
        def readline(self, size=513):
            if self.lines:return super().readline(size)
            if hasattr(self,'pending') and time.monotonic()-self.started >= .55:
                r=self.pending
                del self.pending
                return json.dumps({'id':r['id'],'session':r['session'],'state':'executed','ble_connected':True,'mouse_ready':True}).encode()+b'\n'
            return b''
    wire = DelayedWire()
    b = SerialBackend(wire)
    result = b.execute({'id':'long','session':'session','op':'click','args':{},'deadline':time.monotonic()+1})
    assert result['state'] == 'executed'
    ops = [json.loads(raw)['op'] for raw in wire.writes]
    assert ops.count('click') == 1
    assert 'ping' in ops


@pytest.mark.parametrize('mouse_ready', [False, None])
def test_keyboard_ready_ack_does_not_prove_mouse_report_ready(mouse_ready):
    wire = Wire(mouse_ready=mouse_ready)
    backend = SerialBackend(wire)
    with pytest.raises(ConnectionError):
        backend.execute({'id':'a','session':'s','op':'move','args':{'dx':8,'dy':0},
                         'deadline':time.monotonic()+.1})
    assert [json.loads(raw)['op'] for raw in wire.writes] == ['hello', 'move']


def test_input_guard_runs_after_hello_and_can_prevent_actual_click():
    wire = Wire()
    backend = SerialBackend(wire)
    def guard():
        assert [json.loads(raw)['op'] for raw in wire.writes] == ['hello']
        raise ValueError('page location changed while arming')
    with pytest.raises(ValueError):
        backend.execute({'id':'a','session':'s','op':'click','args':{},
                         'deadline':time.monotonic()+.1,'_pre_input_guard':guard})
    assert [json.loads(raw)['op'] for raw in wire.writes] == ['hello']


def test_successful_guard_is_not_serialized_and_expiry_still_prevents_input():
    wire = Wire()
    backend = SerialBackend(wire)
    calls = []
    request = {'id':'a','session':'s','op':'click','args':{},'deadline':time.monotonic()+.1,
               '_pre_input_guard':lambda:calls.append(True)}
    assert backend.execute(request)['state'] == 'executed'
    assert calls == [True]
    assert all('_pre_input_guard' not in json.loads(raw) for raw in wire.writes)
    def expire():
        request['deadline'] = time.monotonic()-1
    request.update(id='b',_pre_input_guard=expire)
    with pytest.raises(TimeoutError): backend.execute(request)
    assert [json.loads(raw)['op'] for raw in wire.writes] == ['hello','click']


def test_shared_stop_after_hello_blocks_action_and_cleanup_still_sends_stop(tmp_path):
    from phonelab.stop_signal import StopStore
    store = StopStore(tmp_path)
    class StopAfterHello(Wire):
        def readline(self, size=513):
            line = super().readline(size)
            if line and json.loads(self.writes[-1])["op"] == "hello": store.publish_stop()
            return line
    wire = StopAfterHello()
    backend = SerialBackend(wire, stop_token=store.capture())
    with pytest.raises(TimeoutError):
        backend.execute({"id":"once", "session":"one", "op":"click", "args":{},
                         "deadline":time.monotonic()+1})
    backend.close()
    assert [json.loads(raw)["op"] for raw in wire.writes] == ["hello", "stop"]


def test_shared_stop_interrupts_missing_ack_without_retry_or_heartbeat(tmp_path):
    from phonelab.stop_signal import StopStore
    store = StopStore(tmp_path)
    class StopDuringAction(Wire):
        def write(self, raw):
            if json.loads(raw)["op"] == "click":
                self.writes.append(raw)
                store.publish_stop()
            else: super().write(raw)
    wire = StopDuringAction()
    backend = SerialBackend(wire, stop_token=store.capture())
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        backend.execute({"id":"once", "session":"one", "op":"click", "args":{},
                         "deadline":time.monotonic()+3})
    assert time.monotonic()-start < .5
    backend.close()
    assert [json.loads(raw)["op"] for raw in wire.writes] == ["hello", "click", "stop"]


def test_image_preparation_cannot_expand_hid_dispatch_time_bound():
    wire = Wire()
    backend = SerialBackend(wire)
    def guard(): time.sleep(.03)
    backend.execute({"id":"image", "session":"one", "op":"click", "args":{},
        "deadline":time.monotonic()+5, "_pre_input_guard":guard, "_dispatch_ttl_seconds":.02})
    click = json.loads(wire.writes[-1])
    assert click["op"] == "click" and 1 <= click["ttl_ms"] <= 20
    assert "_dispatch_ttl_seconds" not in click and "_pre_input_guard" not in click


def test_guard_frame_expiry_caps_action_ttl_and_never_renews_frame():
    wire = Wire()
    backend = SerialBackend(wire)
    expiry = time.monotonic() + .05
    backend.execute({"id":"image", "session":"one", "op":"click", "args":{},
        "deadline":time.monotonic()+5, "_pre_input_guard":lambda: {"valid_before_monotonic":expiry},
        "_dispatch_ttl_seconds":1})
    assert 1 <= json.loads(wire.writes[-1])["ttl_ms"] <= 50


def test_expired_guard_frame_prevents_action_write():
    wire = Wire()
    backend = SerialBackend(wire)
    with pytest.raises(TimeoutError):
        backend.execute({"id":"image", "session":"one", "op":"click", "args":{},
            "deadline":time.monotonic()+5,
            "_pre_input_guard":lambda: {"valid_before_monotonic":time.monotonic()-1},
            "_dispatch_ttl_seconds":1})
    assert [json.loads(raw)["op"] for raw in wire.writes] == ["hello"]
