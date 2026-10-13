"""Experimental firmware transport. Never attach this to OEM firmware."""
import json
import math
import threading
import time
import uuid


class SerialBackend:
    name = 'esp32-ble-serial-unverified'

    def __init__(self, wire, *, stop_token=None):
        self.wire = wire
        self.session = None
        self.cancel = threading.Event()
        self._write = threading.Lock()
        self.stop_token = stop_token

    @classmethod
    def open_approved(cls, port, approval, *, stop_token=None):
        if not (approval.get('experimental_firmware_written_with_permission') is True and
                approval.get('deployed_firmware_sha256') and approval.get('port') == port):
            raise ValueError('OEM protection: no approved experimental firmware deployment')
        # This call is deliberately absent from normal startup/discovery.
        import serial
        wire = serial.Serial(port=None, baudrate=115200, timeout=.05, write_timeout=.3)
        wire.dtr = False
        wire.rts = False
        wire.port = port
        wire.open()
        return cls(wire, stop_token=stop_token)

    def _shared_stopped(self):
        return self.stop_token is not None and self.stop_token.is_set()

    def _send(self, request):
        with self._write:
            event = request.get('_cancel_event')
            if request['op'] != 'stop' and (self.cancel.is_set() or self._shared_stopped()
                                          or (event is not None and event.is_set())):
                raise TimeoutError('request generation cancelled before write')
            remaining = request['deadline'] - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('request expired immediately before write')
            ttl = max(1, min(5000, int(remaining * 1000)))
            raw = json.dumps({key: request[key] for key in ('id','session','op','args')}, separators=(',',':'))
            raw = raw[:-1] + ',"ttl_ms":' + str(ttl) + '}\n'
            if len(raw.encode()) > 512: raise ValueError('serial line too long')
            self.wire.write(raw.encode('ascii'))

    def _exchange(self, request):
        if time.monotonic() >= request['deadline']: raise TimeoutError('expired before send')
        self._send(request)
        pending = b''
        last_heartbeat = time.monotonic()
        event = request.get('_cancel_event', self.cancel)
        while (time.monotonic() < request['deadline'] and not self.cancel.is_set()
               and not self._shared_stopped() and not event.is_set()):
            if request['op'] not in ('hello','ping','stop') and time.monotonic()-last_heartbeat >= .4:
                self._send({**request,'id':'beat-'+uuid.uuid4().hex,'op':'ping','args':{}})
                last_heartbeat = time.monotonic()
            chunk = self.wire.readline(513)
            if self._shared_stopped() or event.is_set() or self.cancel.is_set():
                raise TimeoutError('generation stopped; action may have occurred; do not replay')
            pending += chunk
            if len(pending) > 512: raise ValueError('oversize serial response')
            if b'\n' not in pending:
                if not chunk: time.sleep(.001)
                continue
            line, _, pending = pending.partition(b'\n')
            try: response = json.loads(line)
            except (ValueError, UnicodeDecodeError): continue
            if not isinstance(response, dict): continue
            if response.get('id') != request['id'] or response.get('session') != request['session']: continue
            if response.get('state') not in ('executed','rejected'): raise ValueError('invalid serial ACK')
            if response.get('state') == 'executed' and response.get('ble_connected') is not True:
                raise ConnectionError('BLE not connected')
            if (response.get('state') == 'executed' and
                    request['op'] in ('move', 'home', 'click', 'press', 'release', 'scroll') and
                    response.get('mouse_ready') is not True):
                raise ConnectionError('mouse HID report readiness is unverified')
            return response
        raise TimeoutError('ACK missing; action may have occurred; do not replay')

    def execute(self, request):
        event = request.get('_cancel_event')
        with self._write:
            if self._shared_stopped() or (event is not None and event.is_set()):
                raise TimeoutError('request generation already cancelled')
            if event is not None and self.session != request['session']:
                self.cancel.clear()  # only an uncancelled new controller generation can rearm
            if self.cancel.is_set(): raise TimeoutError('backend stopped; explicit new generation required')
        if self.session != request['session']:
            hello = {**request, 'id': 'hello-' + uuid.uuid4().hex, 'op': 'hello', 'args': {}}
            response = self._exchange(hello)
            if response['state'] != 'executed': return response
            self.session = request['session']
        guard = request.get('_pre_input_guard')
        if guard is not None:
            if not callable(guard): raise ValueError('invalid local input guard')
            validity = guard()  # recheck page scope after hello, before the actual action write
            if validity is not None:
                expiry = validity.get('valid_before_monotonic') if isinstance(validity, dict) else None
                if type(expiry) not in (int, float) or not math.isfinite(expiry):
                    raise ValueError('invalid local frame validity bound')
                request = {**request, 'deadline': min(request['deadline'], expiry)}
        dispatch_ttl = request.get('_dispatch_ttl_seconds')
        if dispatch_ttl is not None:
            if type(dispatch_ttl) not in (int, float) or not 0 < dispatch_ttl <= 1:
                raise ValueError('invalid local action time bound')
            request = {**request, 'deadline': min(request['deadline'], time.monotonic() + dispatch_ttl)}
        return self._exchange(request)

    def stop(self):
        self.cancel.set()
        sent = False
        try:
            self._send({'id': 'stop-' + uuid.uuid4().hex, 'session': self.session or 'stopped',
                        'op': 'stop', 'args': {}, 'deadline': time.monotonic()+.3})
            sent = True
        except Exception:
            pass
        self.session = None
        return {'release_sent': sent, 'release_confirmed': False}

    def close(self):
        self.stop()
        self.wire.close()
