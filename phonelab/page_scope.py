"""One small pointer movement on the currently visible synthetic page.

This intentionally does not use or weaken the generic Controller's lock gate.
It establishes page visibility/progression, never an OS UNLOCKED state. Separate
one-action experiments require a current page image or browser observation.
Only fixed synthetic text and backspace are exposed; home, holds, queued actions,
and automatic retries are not exposed.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import math
import threading
import time
import uuid

from .visual_marker import VisualMarkerTracker


class PageScopeError(RuntimeError):
    pass


_SYNTHETIC_TEXT_PHRASES = (
    'test123', 'TEST123', 'Test 123', 'test123!@#',
    'ASCII symbols: !"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~',
)


def _synthetic_text_prefix(value):
    return (isinstance(value, str) and value.isascii()
            and any(phrase.startswith(value) for phrase in _SYNTHETIC_TEXT_PHRASES))


@contextmanager
def windows_input_lease(path):
    """Cooperating experiment processes share one exclusive OS byte lock."""
    import msvcrt
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise PageScopeError('input is already leased') from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class _OnePageAction:
    allowed_operations = frozenset(('move', 'click', 'scroll'))
    preparation_seconds = 1.0
    dispatch_seconds = None
    observation_max_age = 2.0
    def __init__(self, observer, *, target, run_id, nonce, test_id,
                 backend_opener, lease_factory, cancel_token, clock=time.monotonic, sleep=time.sleep):
        self.observer = observer
        self.target, self.run_id, self.nonce, self.test_id = target, run_id, nonce, test_id
        self.backend_opener, self.lease_factory = backend_opener, lease_factory
        self.clock, self.sleep = clock, sleep
        if not callable(getattr(cancel_token, 'is_set', None)):
            raise PageScopeError('explicit cancellation token required')
        self.cancel_token = cancel_token
        self._action = threading.Lock()
        self._consumed = False

    def _observe(self, tracker, previous_sequence):
        self._check_cancelled()
        run = self.observer.events(self.run_id)['run']
        if any(run.get(key) != value for key, value in (
                ('run_id', self.run_id), ('nonce', self.nonce), ('test_id', self.test_id))):
            raise PageScopeError('page session changed')
        selection_deadline = self.clock() + 1.5
        while True:
            snapshot = self.observer.snapshot()
            self._check_cancelled()
            meta = snapshot.metadata
            now = self.clock()
            if (meta.get('target') != self.target or meta.get('source') != 'USB_SCREENSHOT'
                or meta.get('orientation') != 'portrait' or meta.get('fresh') is not True
                or meta.get('state') != 'fresh' or meta.get('locked') != 'UNKNOWN'
                or (meta.get('width'), meta.get('height')) != (828, 1792)
                or type(meta.get('sequence')) is not int or meta['sequence'] <= previous_sequence
                or type(meta.get('received_monotonic')) not in (int, float)
                    or not 0 <= now - meta['received_monotonic'] <= 2):
                raise PageScopeError('current synthetic page observation unavailable')
            if now - meta['received_monotonic'] <= self.observation_max_age:
                break
            if now >= selection_deadline:
                raise PageScopeError('new image for bounded processing unavailable')
            self.sleep(.05)
        state = tracker.observe(snapshot.image_bytes, sequence=meta['sequence'],
                                received=meta['received_monotonic'], now=now)
        if state['state'] == 'stale_content':
            raise PageScopeError('synthetic page content is stale')
        return snapshot, state

    def _execute_one(self, operation, arguments):
        if operation not in self.allowed_operations:
            raise PageScopeError('unsupported page experiment')
        if not self._action.acquire(blocking=False):
            raise PageScopeError('an action is already running')
        backend = None
        try:
            if self._consumed:
                raise PageScopeError('one-shot experiment consumed; never replay')
            self._consumed = True
            with self.lease_factory():
                self._check_cancelled()
                tracker = VisualMarkerTracker(self.nonce)
                first, _ = self._observe(tracker, 0)
                self.sleep(.7)
                before, state = self._observe(tracker, first.metadata['sequence'])
                if state['state'] != 'progress_observed':
                    raise PageScopeError('synthetic page progression not observed')
                previous_counter = state['counter']
                self._check_cancelled()
                backend = self.backend_opener()  # repeats actual deployment/identity gates
                self._check_cancelled()
                # Opening UART may take seconds; never act on that earlier frame.
                self.sleep(.7)
                before, state = self._observe(tracker, before.metadata['sequence'])
                if state['state'] != 'progress_observed' or state['counter'] <= previous_counter:
                    raise PageScopeError('synthetic page progression lost before input')
                previous_counter = state['counter']
                self._before_hello(before)
                def final_input_guard():
                    nonlocal before, previous_counter
                    self._check_cancelled()
                    # Recheck after the serial hello as well. The same capture
                    # sequence is allowed here, but its receipt/content age and
                    # marker must still be valid; this never renews timestamps.
                    before, guarded_state = self._observe(tracker, before.metadata['sequence'] - 1)
                    previous_counter = guarded_state['counter']
                    self._before_dispatch(before)
                    return {'valid_before_monotonic': before.metadata['received_monotonic'] + 2}
                request = {'id': uuid.uuid4().hex, 'session': uuid.uuid4().hex,
                           'owner': 'onsite-synthetic-experiment', 'op': operation,
                           'args': arguments, 'deadline': self.clock() + self.preparation_seconds,
                           '_pre_input_guard': final_input_guard,
                           '_cancel_event': self.cancel_token}
                if self.dispatch_seconds is not None:
                    request['_dispatch_ttl_seconds'] = self.dispatch_seconds
                self._check_cancelled()
                result = backend.execute(request)
                self._check_cancelled()
                if (not isinstance(result, dict) or result.get('state') != 'executed'
                        or result.get('ble_connected') is not True):
                    raise PageScopeError('action outcome unknown or rejected; do not replay')
                # Close before observing results; the experiment never leaves an armed port.
                backend.close(); backend = None
                self.sleep(.7)
                after, state = self._observe(tracker, before.metadata['sequence'])
                if state['state'] != 'progress_observed' or state['counter'] <= previous_counter:
                    raise PageScopeError('post-action observation unavailable; do not replay')
                return {'state': 'executed', 'verified': False,
                        'interaction_scope': 'current_synthetic_page_only',
                        'lock_state': 'UNKNOWN', 'before': before, 'after': after,
                        'request_id': request['id'], 'op': operation, 'args': arguments}
        except PageScopeError:
            raise
        except Exception:
            raise PageScopeError('experiment stopped; outcome may be unknown; do not replay') from None
        finally:
            if backend is not None:
                try: backend.close()
                except Exception: pass
            self._action.release()

    def _before_dispatch(self, snapshot=None):
        pass

    def _before_hello(self, snapshot=None):
        # This hook runs after UART open but before serial hello. Preserve
        # existing early checks for ordinary scopes; image-located scopes may
        # defer costly pixel/OCR checks to the mandatory post-hello guard.
        self._before_dispatch(snapshot)

    def _check_cancelled(self):
        if self.cancel_token.is_set():
            raise PageScopeError('experiment generation stopped; explicit new invocation required')


class PageMovementExperiment(_OnePageAction):
    def move(self, dx, dy):
        if any(type(v) is not int or not -16 <= v <= 16 for v in (dx, dy)) or (dx, dy) == (0, 0):
            raise PageScopeError('a nonzero movement of at most 16 per axis is required')
        return self._execute_one('move', {'dx': dx, 'dy': dy})


@dataclass(frozen=True)
class OnsitePointerConfirmation:
    target: str
    run_id: str
    probe: str
    recorded_monotonic: float
    source: str


class PageConfirmedClickExperiment(_OnePageAction):
    """One click after an onsite report that the pointer is on the test grid.

    A human location report is neither coordinate calibration nor OS lock proof.
    This deliberately has no move/type/key interface or automatic repeat.
    """
    def click(self, confirmation):
        self._confirmation = confirmation
        self._before_dispatch()
        return self._execute_one('click', {})

    def _before_dispatch(self, snapshot=None):
        confirmation = self._confirmation
        if (not isinstance(confirmation, OnsitePointerConfirmation)
                or confirmation.source != 'ONSITE_USER_CONFIRMED'
                or confirmation.target != self.target or confirmation.run_id != self.run_id
                or confirmation.probe not in {f'{n:02d}' for n in range(1, 26)}
                or type(confirmation.recorded_monotonic) not in (int, float)
                or not math.isfinite(confirmation.recorded_monotonic)
                or not 0 <= self.clock() - confirmation.recorded_monotonic <= 180):
            raise PageScopeError('fresh onsite pointer confirmation for this page is required')


class _PageObservedPointerAction(_OnePageAction):
    """One action at a recent browser-reported pointer location on this test grid.

    This reads the authenticated observation endpoint itself, both before opening
    UART and just before dispatch. Any newer event, including a viewport change,
    invalidates the location. Browser isTrusted is not hardware or OS lock proof;
    the progressing real PNG marker independently limits the action to this page.
    """
    def _at_pointer(self, pointer_sequence, operation, arguments):
        self._pointer_sequence = pointer_sequence
        self._before_dispatch()
        return self._execute_one(operation, arguments)

    def _before_dispatch(self, snapshot=None):
        if type(self._pointer_sequence) is not int or self._pointer_sequence < 1:
            raise PageScopeError('pointer event sequence required')
        observations = self.observer.events(self.run_id)
        run, rows = observations['run'], observations['events']
        if (any(run.get(key) != value for key, value in (
                ('run_id', self.run_id), ('nonce', self.nonce), ('test_id', self.test_id)))
                or not rows or rows[-1].get('seq') != self._pointer_sequence):
            raise PageScopeError('pointer event is no longer the current page observation')
        row = rows[-1]
        event = row.get('event', {})
        recorded = row.get('recorded_monotonic')
        if (row.get('source') != 'client_reported_browser_event'
                or row.get('run_id') != self.run_id or row.get('test_id') != self.test_id
                or type(recorded) not in (int, float) or not math.isfinite(recorded)
                or not 0 <= self.clock() - recorded <= 10
                or event.get('type') != 'pointermove' or event.get('isTrusted') is not True
                or event.get('target') not in {'probe-grid', *(f'{n:02d}' for n in range(1, 26))}
                or event.get('button') != -1
                or any(type(event.get(axis)) not in (int, float)
                       or not math.isfinite(event[axis]) or not 16 < event[axis] < maximum
                       for axis, maximum in (('clientX', 398), ('clientY', 698)))):
            raise PageScopeError('recent trusted pointer event within the test grid is required')


class PageObservedClickExperiment(_PageObservedPointerAction):
    """One click; any result remains unverified until independent observation."""
    def click(self, pointer_sequence):
        return self._at_pointer(pointer_sequence, 'click', {})


class PageObservedScrollExperiment(_PageObservedPointerAction):
    """One small vertical wheel action on the observed synthetic test grid.

    Scrolling invalidates prior screen/CSS coordinate calibration. A caller must
    observe the resulting page and viewport before any further coordinate action.
    """
    def scroll(self, pointer_sequence, vertical):
        if type(vertical) is not int or vertical == 0 or not -3 <= vertical <= 3:
            raise PageScopeError('one nonzero vertical wheel action of at most 3 is required')
        return self._at_pointer(pointer_sequence, 'scroll', {'v': vertical, 'h': 0})


class _PageImageLocatedActionExperiment(_OnePageAction):
    """Shared fresh-image guard and one-second dispatch for a page action."""
    preparation_seconds = 5.0
    dispatch_seconds = 1.0
    observation_max_age = .25

    def __init__(self, *args, image_guard, expected_count=None, probe_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.image_guard = image_guard
        self.expected_count = expected_count
        self.probe_id = probe_id

    def _before_hello(self, snapshot=None):
        if snapshot is None:
            raise PageScopeError('fresh image receipt required')
        received = snapshot.metadata.get('received_monotonic')
        if (type(received) not in (int, float) or not math.isfinite(received)
                or not 0 <= self.clock() - received <= 2):
            raise PageScopeError('image receipt expired before serial hello')
        self._check_cancelled()

    def _before_dispatch(self, snapshot=None):
        if snapshot is None:
            raise PageScopeError('fresh image location required')
        observed = self.image_guard.verify(snapshot.image_bytes)
        if self.expected_count is not None:
            if (not isinstance(observed, dict) or observed.get('probe_id') != self.probe_id
                    or observed.get('count') != self.expected_count):
                raise PageScopeError('current target image/count changed')
        if not 0 <= self.clock() - snapshot.metadata['received_monotonic'] <= 2:
            raise PageScopeError('image location expired during validation')
        self._check_cancelled()
        return observed


class PageImageLocatedClickExperiment(_PageImageLocatedActionExperiment):
    """One click located by fresh pixels, without requiring browser hover events."""
    def click(self):
        return self._execute_one('click', {})


class PageImageLocatedScrollExperiment(_PageImageLocatedActionExperiment):
    """One small wheel action while the pointer is image-verified on a probe."""
    def __init__(self, *args, probe_id, expected_count, **kwargs):
        if (not isinstance(probe_id, str) or probe_id not in {f'{n:02d}' for n in range(1, 26)}
                or type(expected_count) is not int or not 0 <= expected_count <= 999999):
            raise PageScopeError('current synthetic probe id and count are required')
        super().__init__(*args, probe_id=probe_id, expected_count=expected_count, **kwargs)

    def scroll(self, vertical):
        if type(vertical) is not int or vertical == 0 or not -3 <= vertical <= 3:
            raise PageScopeError('one nonzero vertical wheel action of at most 3 is required')
        return self._execute_one('scroll', {'v': vertical, 'h': 0})


class PageImageLocatedTextExperiment(_PageImageLocatedActionExperiment):
    """One allowlisted text/key action on the focused synthetic input only."""
    allowed_operations = frozenset(('type', 'key'))
    preparation_seconds = 5.0
    dispatch_seconds = 1.0
    observation_max_age = .25

    def __init__(self, *args, image_guard, expected_text, **kwargs):
        if not _synthetic_text_prefix(expected_text):
            raise PageScopeError('expected field text is not an approved synthetic phrase prefix')
        super().__init__(*args, image_guard=image_guard, **kwargs)
        self.expected_text = expected_text

    def _before_dispatch(self, snapshot=None):
        observed = super()._before_dispatch(snapshot)
        if (not isinstance(observed, dict)
                or observed.get('role') != 'synthetic-text'
                or observed.get('focused') is not True
                or observed.get('caret_at_end') is not True
                or not isinstance(observed.get('text'), str)
                or observed.get('text') != self.expected_text):
            raise PageScopeError('focused synthetic text field changed or is ambiguous')

    def type_phrase(self, phrase):
        if self.expected_text != '' or not isinstance(phrase, str) or phrase not in _SYNTHETIC_TEXT_PHRASES:
            raise PageScopeError('typing requires an empty field and one approved synthetic phrase')
        return self._execute_one('type', {'text': phrase})

    def backspace(self):
        if not self.expected_text or not _synthetic_text_prefix(self.expected_text):
            raise PageScopeError('backspace requires a nonempty approved synthetic phrase prefix')
        return self._execute_one('key', {'key': 'backspace'})
