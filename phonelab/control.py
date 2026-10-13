"""Fail-closed control gate; a transport ACK is never visual verification."""
from dataclasses import dataclass
import math
import re
import secrets
import threading
import time


class LabError(ValueError):
    pass


TOKEN = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')


def validate(request):
    if not isinstance(request, dict):
        raise LabError('request must be an object')
    for key in ('id', 'session', 'owner'):
        if not isinstance(request.get(key), str) or not TOKEN.fullmatch(request[key]):
            raise LabError('invalid ' + key)
    deadline = request.get('deadline')
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise LabError('invalid deadline')
    if not time.monotonic() < deadline <= time.monotonic() + 5:
        raise LabError('expired or excessive deadline')
    op = request.get('op')
    a = request.get('args')
    if not isinstance(a, dict): raise LabError('args must be an object')
    specs = {'move': {'dx': (-2048, 2048), 'dy': (-2048, 2048)},
             'scroll': {'v': (-127, 127), 'h': (-127, 127)}}
    if op in specs:
        if set(a) != set(specs[op]): raise LabError('unexpected arguments')
        for key, (low, high) in specs[op].items():
            if type(a[key]) is not int or not low <= a[key] <= high:
                raise LabError('invalid ' + key)
    elif op == 'type':
        text = a.get('text')
        if set(a) != {'text'} or not isinstance(text, str) or not 1 <= len(text) <= 128:
            raise LabError('invalid text')
        if any(not 32 <= ord(c) <= 126 for c in text):
            raise LabError('only printable ASCII is supported')
    elif op == 'key':
        if set(a) != {'key'} or a['key'] not in ('enter', 'backspace', 'escape', 'tab'):
            raise LabError('unsupported key')
    elif op in ('click', 'press', 'release', 'home', 'ping'):
        if a: raise LabError('unexpected arguments')
    else:
        raise LabError('unsupported operation')


@dataclass
class Frame:
    target: str
    sequence: int
    received_monotonic: float
    width: int
    height: int
    orientation: str
    source_time: str | None = None
    source: str = 'UNKNOWN'
    locked: bool | None = None


class Controller:
    def __init__(self, target, backend=None, freshness_seconds=2):
        self.target = target
        self.backend = backend
        self.freshness_seconds = freshness_seconds
        self.session = secrets.token_hex(16)
        self.owner = None
        self.frame = None
        self.calibration = None
        self.armed = False
        self.seen = set()
        self.reason = 'not prepared'
        self._cancel_event = threading.Event()
        self._state = threading.RLock()
        self._action = threading.Lock()

    def acquire(self, owner):
        if not isinstance(owner, str) or not TOKEN.fullmatch(owner): raise LabError('invalid owner')
        with self._state:
            if self.owner and self.owner != owner: raise LabError('controller already leased')
            if self.owner is None: self._cancel_event = threading.Event()
            self.owner = owner
            return self.session

    def set_frame(self, frame):
        unsafe = False
        with self._state:
            if self.frame and frame.target == self.frame.target and frame.sequence <= self.frame.sequence:
                raise LabError('non-increasing capture sequence')
            self.frame = frame
            if (frame.target != self.target or frame.locked is not False or
                frame.orientation != 'portrait' or
                (self.calibration and self.calibration != (frame.width, frame.height, frame.orientation))):
                self.calibration = None
                self.armed = False
                unsafe = True
        if unsafe: self.stop()

    def _fresh(self):
        f = self.frame
        return bool(f and f.target == self.target and f.locked is False and
                    f.orientation == 'portrait' and 0 <= time.monotonic() - f.received_monotonic <= self.freshness_seconds)

    def calibrate(self, width, height, orientation):
        with self._state:
            if not self._fresh() or not self.owner or not self.backend: raise LabError('fresh unlocked target and backend required')
            if (width, height, orientation) != (self.frame.width, self.frame.height, self.frame.orientation):
                raise LabError('frame geometry mismatch')
            self.calibration = (width, height, orientation)
            self.armed = True

    def disconnect(self, reason):
        with self._state:
            self._cancel_event.set()
            self.armed = False
            self.calibration = None
            self.owner = None
            self.session = secrets.token_hex(16)
            self.seen.clear()
            self.reason = reason

    def stop(self):
        self.disconnect('stopped')
        try:
            result = self.backend.stop() if self.backend else {'release_sent': False, 'release_confirmed': False}
        except Exception:
            result = {'release_sent': False, 'release_confirmed': False}
        return {'state': 'stopped', **result}

    def get_status(self):
        with self._state:
            return {'target': self.target, 'session': self.session, 'owner': self.owner,
                    'armed': self.armed, 'reason': self.reason,
                    'ACTIVE_INPUT_BACKEND': getattr(self.backend, 'name', 'none'),
                    'INPUT_AVAILABLE': 'UNKNOWN',  # only independent phone event evidence can promote this
                    'FRESH_SCREEN_AVAILABLE': 'VERIFIED' if self._fresh() else 'UNKNOWN',
                    'calibrated': bool(self.calibration)}

    def watchdog(self):
        with self._state:
            unsafe = self.armed and (not self._fresh() or not self.calibration)
        if unsafe: return self.stop()
        return {'state': 'idle', 'armed': self.armed}

    def submit(self, request):
        validate(request)
        if not self._action.acquire(blocking=False): raise LabError('action already in progress')
        try:
            with self._state:
                if not self.backend: raise LabError('no real input backend')
                if request['owner'] != self.owner or request['session'] != self.session:
                    raise LabError('lease or generation mismatch')
                if not self.armed or not self.calibration or not self._fresh():
                    self.stop()
                    raise LabError('observation/calibration invalid')
                if request['id'] in self.seen: raise LabError('duplicate id; do not retry non-idempotent input')
                if len(self.seen) >= 10000: raise LabError('session request limit reached')
                self.seen.add(request['id'])
                generation = self.session
                cancel_event = self._cancel_event
            try:
                result = self.backend.execute({**request, '_cancel_event': cancel_event})
            except Exception:
                self.stop()
                return {'id': request['id'], 'state': 'unknown', 'verified': False, 'error': 'transport failure; input may have occurred'}
            with self._state:
                if self.session != generation:
                    return {'id': request['id'], 'state': 'unknown', 'verified': False, 'error': 'cancelled or disconnected'}
            if not isinstance(result, dict) or result.get('state') not in ('executed', 'rejected'):
                self.stop()
                return {'id': request['id'], 'state': 'unknown', 'verified': False, 'error': 'invalid acknowledgement'}
            return {**result, 'id': request['id'], 'verified': False}
        finally:
            self._action.release()
