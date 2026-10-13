"""Safety behavior of the one-action, synthetic-page-only movement experiment."""
from contextlib import nullcontext
from types import SimpleNamespace
import threading

import pytest

from phonelab.page_scope import (PageScopeError, PageMovementExperiment,
                                PageConfirmedClickExperiment, OnsitePointerConfirmation)
import phonelab.page_scope as page_scope
from test_visual_marker import fixture


class Clock:
    now = 10.
    def __call__(self): return self.now
    def sleep(self, seconds): self.now += seconds


class Observer:
    def __init__(self, clock): self.clock = clock; self.seq = 0; self.target = 'test-target'; self.frozen = False
    def snapshot(self):
        self.seq += 1
        png, _ = fixture(1 if self.frozen else self.seq)
        return SimpleNamespace(image_bytes=png, metadata={
            'target': self.target, 'source': 'USB_SCREENSHOT', 'sequence': self.seq,
            'received_monotonic': self.clock(), 'width': 828, 'height': 1792,
            'orientation': 'portrait', 'locked': 'UNKNOWN', 'state': 'fresh', 'fresh': True})
    def events(self, run_id):
        return {'run': {'run_id': run_id, 'nonce': 'hello', 'test_id': 'ble-round-01'}, 'events': []}


class Backend:
    def __init__(self): self.calls = []; self.closed = 0; self.result = {'state': 'executed', 'ble_connected': True}
    def execute(self, request): self.calls.append(request); return self.result
    def close(self): self.closed += 1


def setup():
    clock = Clock(); observer = Observer(clock); backend = Backend(); opened = []
    experiment = PageMovementExperiment(observer, target='test-target', run_id='a'*16,
        nonce='hello', test_id='ble-round-01', backend_opener=lambda: opened.append(True) or backend,
        lease_factory=lambda: nullcontext(), clock=clock, sleep=clock.sleep,
        cancel_token=threading.Event())
    return experiment, observer, backend, opened


def test_one_small_move_needs_progress_and_never_claims_unlock_or_input_verification():
    experiment, observer, backend, opened = setup()
    result = experiment.move(8, -4)
    assert opened == [True] and len(backend.calls) == 1 and backend.closed == 1
    assert backend.calls[0]['op'] == 'move' and backend.calls[0]['args'] == {'dx': 8, 'dy': -4}
    assert result['verified'] is False and result['lock_state'] == 'UNKNOWN'
    assert result['interaction_scope'] == 'current_synthetic_page_only'


@pytest.mark.parametrize('dx,dy', [(17, 0), (0, -17), (True, 0), (0, 0)])
def test_excessive_or_invalid_delta_never_opens_serial(dx, dy):
    experiment, observer, backend, opened = setup()
    with pytest.raises(PageScopeError): experiment.move(dx, dy)
    assert not opened and not backend.calls


@pytest.mark.parametrize('fault', ['wrong_target', 'frozen', 'wrong_run', 'stale'])
def test_invalid_scope_never_opens_serial(fault):
    experiment, observer, backend, opened = setup()
    if fault == 'wrong_target': observer.target = 'other-phone'
    if fault == 'frozen': observer.frozen = True
    if fault == 'wrong_run': observer.events = lambda _: {'run': {'nonce': 'other'}, 'events': []}
    if fault == 'stale':
        original = observer.snapshot
        def stale():
            result = original(); result.metadata['received_monotonic'] -= 10; return result
        observer.snapshot = stale
    with pytest.raises(PageScopeError): experiment.move(8, 0)
    assert not opened and not backend.calls


def test_scope_lost_during_backend_open_closes_without_action():
    experiment, observer, backend, opened = setup()
    def open_then_lose(): observer.target = 'other-phone'; return backend
    experiment.backend_opener = open_then_lose
    with pytest.raises(PageScopeError): experiment.move(8, 0)
    assert not backend.calls and backend.closed == 1


def test_explicit_locked_state_never_opens_even_if_a_marker_is_present():
    experiment, observer, backend, opened = setup()
    original = observer.snapshot
    def locked():
        result = original(); result.metadata['locked'] = 'LOCKED'; return result
    observer.snapshot = locked
    with pytest.raises(PageScopeError): experiment.move(8, 0)
    assert not opened and not backend.calls


def test_unknown_ack_is_not_replayed_and_closes_backend():
    experiment, observer, backend, opened = setup()
    backend.result = {'state': 'unknown'}
    with pytest.raises(PageScopeError): experiment.move(8, 0)
    assert len(backend.calls) == 1 and backend.closed == 1
    with pytest.raises(PageScopeError): experiment.move(8, 0)
    assert len(backend.calls) == 1 and opened == [True]


def test_successful_experiment_is_one_shot():
    experiment, observer, backend, opened = setup()
    experiment.move(8, 0)
    with pytest.raises(PageScopeError): experiment.move(8, 0)
    assert len(backend.calls) == 1 and opened == [True]


@pytest.mark.parametrize('freeze_at', [3, 4])
def test_recent_progress_cannot_substitute_for_new_pre_or_post_action_progress(freeze_at):
    experiment, observer, backend, opened = setup()
    original = observer.snapshot
    def snapshot():
        result = original()
        if observer.seq >= freeze_at:
            result.image_bytes = fixture(freeze_at - 1)[0]
        return result
    observer.snapshot = snapshot
    with pytest.raises(PageScopeError): experiment.move(8, 0)
    assert len(backend.calls) == (0 if freeze_at == 3 else 1)
    with pytest.raises(PageScopeError): experiment.move(8, 0)
    assert opened == [True] and backend.closed == 1


def confirmed_click_setup():
    movement, observer, backend, opened = setup()
    click = PageConfirmedClickExperiment(observer, target=movement.target, run_id=movement.run_id,
        nonce=movement.nonce, test_id=movement.test_id, backend_opener=movement.backend_opener,
        lease_factory=movement.lease_factory, clock=movement.clock, sleep=movement.sleep,
        cancel_token=movement.cancel_token)
    confirmation = OnsitePointerConfirmation('test-target', 'a'*16, '12', 10., 'ONSITE_USER_CONFIRMED')
    return click, confirmation, observer, backend, opened


def test_confirmed_click_is_single_and_stays_unverified_until_independent_assertion():
    click, confirmation, observer, backend, opened = confirmed_click_setup()
    result = click.click(confirmation)
    assert result['op'] == 'click' and result['verified'] is False
    assert len(backend.calls) == 1 and backend.calls[0]['args'] == {} and backend.closed == 1
    with pytest.raises(PageScopeError): click.click(confirmation)
    assert len(backend.calls) == 1 and opened == [True]


@pytest.mark.parametrize('confirmation', [None,
    OnsitePointerConfirmation('other', 'a'*16, '12', 10., 'ONSITE_USER_CONFIRMED'),
    OnsitePointerConfirmation('test-target', 'b'*16, '12', 10., 'ONSITE_USER_CONFIRMED'),
    OnsitePointerConfirmation('test-target', 'a'*16, 'Safari-toolbar', 10., 'ONSITE_USER_CONFIRMED'),
    OnsitePointerConfirmation('test-target', 'a'*16, '12', -200., 'ONSITE_USER_CONFIRMED'),
    OnsitePointerConfirmation('test-target', 'a'*16, '12', 10., 'GUESSED_FROM_ACK')])
def test_missing_or_wrong_pointer_scope_never_opens(confirmation):
    click, _, observer, backend, opened = confirmed_click_setup()
    with pytest.raises(PageScopeError): click.click(confirmation)
    assert not opened and not backend.calls


def test_pointer_confirmation_expiring_during_open_prevents_click():
    click, confirmation, observer, backend, opened = confirmed_click_setup()
    def delayed_open():
        observer.clock.sleep(180)
        return backend
    click.backend_opener = delayed_open
    with pytest.raises(PageScopeError): click.click(confirmation)
    assert not backend.calls and backend.closed == 1


def observed_click_setup():
    from phonelab.page_scope import PageObservedClickExperiment
    movement, observer, backend, opened = setup()
    row = {'source': 'client_reported_browser_event', 'run_id': movement.run_id,
           'test_id': movement.test_id, 'seq': 3, 'recorded_monotonic': 10.,
           'event': {'type': 'pointermove', 'isTrusted': True, 'target': 'probe-grid',
                     'clientX': 219., 'clientY': 628., 'button': -1}}
    original = observer.events
    observer.rows = [row]
    observer.events = lambda run: dict(original(run), events=observer.rows)
    click = PageObservedClickExperiment(observer, target=movement.target, run_id=movement.run_id,
        nonce=movement.nonce, test_id=movement.test_id, backend_opener=movement.backend_opener,
        lease_factory=movement.lease_factory, clock=movement.clock, sleep=movement.sleep,
        cancel_token=movement.cancel_token)
    return click, observer, backend, opened


class TextGuard:
    def __init__(self, result=None):
        self.result = result
        self.images = []
    def verify(self, png):
        self.images.append(png)
        if callable(self.result):
            return self.result(len(self.images))
        return self.result


def text_setup(*, expected_text="", guard=None, backend=None, cancel_token=None):
    movement, observer, backend, opened = setup()
    backend = backend or Backend()
    def execute(request):
        request["_pre_input_guard"]()
        backend.calls.append(request)
        return backend.result
    backend.execute = execute
    experiment = page_scope.PageImageLocatedTextExperiment(observer, target=movement.target,
        run_id=movement.run_id, nonce=movement.nonce, test_id=movement.test_id,
        backend_opener=lambda: opened.append(True) or backend,
        lease_factory=movement.lease_factory, cancel_token=cancel_token or movement.cancel_token,
        clock=movement.clock, sleep=movement.sleep, image_guard=guard or TextGuard({
            "role": "synthetic-text", "focused": True, "caret_at_end": True,
            "text": expected_text}), expected_text=expected_text)
    return experiment, observer, backend, opened


def test_image_text_type_accepts_only_fixed_phrase_from_empty_field_and_stays_unverified():
    guard = TextGuard({"role": "synthetic-text", "focused": True,
        "caret_at_end": True, "text": ""})
    experiment, observer, backend, opened = text_setup(guard=guard)
    result = experiment.type_phrase("test123!@#")
    assert opened == [True] and len(backend.calls) == 1 and backend.closed == 1
    request = backend.calls[0]
    assert request["op"] == "type" and request["args"] == {"text": "test123!@#"}
    assert request["_dispatch_ttl_seconds"] == 1.0
    assert len(guard.images) == 1
    assert result["verified"] is False and result["lock_state"] == "UNKNOWN"


def test_image_text_type_requires_empty_expected_field_and_exact_allowlisted_phrase():
    experiment, observer, backend, opened = text_setup(expected_text="T")
    with pytest.raises(PageScopeError):
        experiment.type_phrase("test123")
    experiment, observer, backend, opened = text_setup()
    with pytest.raises(PageScopeError):
        experiment.type_phrase("unapproved")
    assert not opened and not backend.calls


def test_image_text_scope_has_no_default_allowing_location_guard_bypass():
    movement, observer, backend, opened = setup()
    with pytest.raises(TypeError):
        page_scope.PageImageLocatedTextExperiment(observer, target=movement.target,
            run_id=movement.run_id, nonce=movement.nonce, test_id=movement.test_id,
            backend_opener=movement.backend_opener, lease_factory=movement.lease_factory,
            cancel_token=movement.cancel_token, clock=movement.clock, sleep=movement.sleep,
            expected_text="")
    assert not opened and not backend.calls


@pytest.mark.parametrize("operation,arguments", [
    ("type", {"text": "test123"}),
    ("key", {"key": "backspace"}),
])
@pytest.mark.parametrize("experiment_kind", ["movement", "image_click"])
def test_non_text_experiments_cannot_dispatch_text_or_key_operations(
        operation, arguments, experiment_kind):
    movement, observer, backend, opened = setup()
    if experiment_kind == "movement":
        experiment = movement
    else:
        experiment = page_scope.PageImageLocatedClickExperiment(
            observer, target=movement.target, run_id=movement.run_id,
            nonce=movement.nonce, test_id=movement.test_id,
            backend_opener=movement.backend_opener, lease_factory=movement.lease_factory,
            cancel_token=movement.cancel_token, clock=movement.clock, sleep=movement.sleep,
            image_guard=TextGuard({"probe_id": "01", "count": 0}))
    with pytest.raises(PageScopeError, match="unsupported page experiment"):
        experiment._execute_one(operation, arguments)
    assert not opened and not backend.calls


@pytest.mark.parametrize("operation,arguments", [
    ("move", {"dx": 1, "dy": 0}),
    ("click", {}),
    ("scroll", {"v": 1, "h": 0}),
])
def test_image_text_experiment_cannot_dispatch_other_action_kinds(operation, arguments):
    experiment, observer, backend, opened = text_setup(expected_text="")
    with pytest.raises(PageScopeError, match="unsupported page experiment"):
        experiment._execute_one(operation, arguments)
    assert not opened and not backend.calls


@pytest.mark.parametrize("prefix", ["test", "test123", "TEST123", "Test 123", "test123!@#",
    "ASCII symbols: !\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"])
def test_image_text_backspace_accepts_nonempty_prefix_and_sends_one_key(prefix):
    guard = TextGuard({"role": "synthetic-text", "focused": True,
        "caret_at_end": True, "text": prefix})
    experiment, observer, backend, opened = text_setup(expected_text=prefix, guard=guard)
    result = experiment.backspace()
    assert result["verified"] is False and len(backend.calls) == 1
    assert backend.calls[0]["op"] == "key" and backend.calls[0]["args"] == {"key": "backspace"}
    assert backend.closed == 1 and len(guard.images) == 1


@pytest.mark.parametrize("text", ["not approved", "test123\n"])
def test_image_text_backspace_rejects_non_allowlisted_field_before_open(text):
    with pytest.raises(PageScopeError):
        text_setup(expected_text=text)


def test_image_text_backspace_rejects_empty_prefix_before_dispatch():
    experiment, observer, backend, opened = text_setup(expected_text="")
    with pytest.raises(PageScopeError): experiment.backspace()
    assert not opened and not backend.calls


@pytest.mark.parametrize("guard_result", [
    {"role": "other", "focused": True, "caret_at_end": True, "text": ""},
    {"role": "synthetic-text", "focused": False, "caret_at_end": True, "text": ""},
    {"role": "synthetic-text", "focused": True, "caret_at_end": False, "text": ""},
    {"role": "synthetic-text", "focused": True, "caret_at_end": True, "text": "wrong"},
    None,
])
def test_image_text_guard_mismatch_closes_without_dispatch(guard_result):
    guard = TextGuard(guard_result)
    experiment, observer, backend, opened = text_setup(guard=guard)
    with pytest.raises(PageScopeError):
        experiment.type_phrase("test123")
    assert opened == [True] and backend.calls == [] and backend.closed == 1
    assert len(guard.images) == 1


def test_image_text_final_guard_catches_focus_change_after_serial_hello():
    guard = TextGuard(lambda attempt: {"role": "synthetic-text", "focused": False,
        "caret_at_end": True, "text": ""})
    experiment, observer, backend, opened = text_setup(guard=guard)
    transport_steps = []
    def hello_then_guard(request):
        transport_steps.append("hello")
        request["_pre_input_guard"]()
        transport_steps.append("dispatch")
        backend.calls.append(request)
        return backend.result
    backend.execute = hello_then_guard
    with pytest.raises(PageScopeError):
        experiment.type_phrase("test123")
    assert transport_steps == ["hello"]
    assert len(guard.images) == 1 and backend.calls == [] and backend.closed == 1


def test_image_text_cancel_after_open_closes_without_action_or_retry():
    cancel = threading.Event()
    experiment, observer, backend, opened = text_setup(cancel_token=cancel)
    def stop_before_guard(request):
        cancel.set()
        request["_pre_input_guard"]()
    backend.execute = stop_before_guard
    with pytest.raises(PageScopeError):
        experiment.type_phrase("test123")
    assert backend.calls == [] and backend.closed == 1
    with pytest.raises(PageScopeError):
        experiment.type_phrase("test123")
    assert len(opened) == 1


def test_image_text_unknown_dispatch_is_closed_and_never_replayed():
    experiment, observer, backend, opened = text_setup()
    backend.result = {"state": "unknown"}
    with pytest.raises(PageScopeError):
        experiment.type_phrase("test123")
    assert len(backend.calls) == 1 and backend.closed == 1
    with pytest.raises(PageScopeError):
        experiment.type_phrase("test123")
    assert len(backend.calls) == 1 and len(opened) == 1


def test_image_text_dispatch_timeout_closes_and_never_replays():
    experiment, observer, backend, opened = text_setup()
    def timeout(request):
        request["_pre_input_guard"]()
        backend.calls.append(request)
        observer.clock.sleep(1.1)
        raise TimeoutError("dispatch deadline elapsed")
    backend.execute = timeout
    with pytest.raises(PageScopeError): experiment.type_phrase("test123")
    assert len(backend.calls) == 1 and backend.closed == 1
    with pytest.raises(PageScopeError): experiment.type_phrase("test123")
    assert len(backend.calls) == 1 and len(opened) == 1


def test_observed_click_fetches_current_pointer_event_and_is_one_shot():
    click, observer, backend, opened = observed_click_setup()
    result = click.click(3)
    assert result['verified'] is False and result['op'] == 'click'
    assert len(backend.calls) == 1 and backend.closed == 1
    with pytest.raises(PageScopeError): click.click(3)
    assert len(backend.calls) == 1


@pytest.mark.parametrize('fault', ['missing', 'stale', 'future', 'untrusted', 'outside',
                                   'wrong_run', 'wrong_test', 'wrong_source', 'bad_coordinate',
                                   'new_event', 'invalid_seq'])
def test_observed_click_requires_recent_unchanged_same_page_grid_event(fault):
    click, observer, backend, opened = observed_click_setup()
    row = observer.rows[0]
    sequence = 3
    if fault == 'missing': observer.rows.clear()
    if fault == 'stale': row['recorded_monotonic'] = -1.
    if fault == 'future': row['recorded_monotonic'] = 11.
    if fault == 'untrusted': row['event']['isTrusted'] = False
    if fault == 'outside': row['event']['target'] = 'Safari-toolbar'
    if fault == 'wrong_run': row['run_id'] = 'b'*16
    if fault == 'wrong_test': row['test_id'] = 'other'
    if fault == 'wrong_source': row['source'] = 'guessed_from_ack'
    if fault == 'bad_coordinate': row['event']['clientX'] = float('nan')
    if fault == 'new_event': observer.rows.append(dict(row, seq=4, event={'type': 'scroll'}))
    if fault == 'invalid_seq': sequence = True
    with pytest.raises(PageScopeError): click.click(sequence)
    assert not opened and not backend.calls


@pytest.mark.parametrize('fault', ['expires', 'moves', 'geometry_changes'])
def test_observed_click_rechecks_location_after_serial_open(fault):
    click, observer, backend, opened = observed_click_setup()
    def change_during_open():
        opened.append(True)
        if fault == 'expires': observer.clock.sleep(10)
        else:
            row = observer.rows[0]
            observer.rows.append(dict(row, seq=4, event={'type': 'pointermove' if fault == 'moves' else 'page_geometry'}))
        return backend
    click.backend_opener = change_during_open
    with pytest.raises(PageScopeError): click.click(3)
    assert not backend.calls and backend.closed == 1


def observed_scroll_setup():
    from phonelab.page_scope import PageObservedScrollExperiment
    click, observer, backend, opened = observed_click_setup()
    scroll = PageObservedScrollExperiment(observer, target=click.target, run_id=click.run_id,
        nonce=click.nonce, test_id=click.test_id, backend_opener=click.backend_opener,
        lease_factory=click.lease_factory, clock=click.clock, sleep=click.sleep,
        cancel_token=click.cancel_token)
    return scroll, observer, backend, opened


def test_small_scroll_is_one_shot_on_the_recent_observed_test_grid():
    scroll, observer, backend, opened = observed_scroll_setup()
    result = scroll.scroll(3, -2)
    assert result['op'] == 'scroll' and result['verified'] is False
    assert backend.calls[0]['args'] == {'v': -2, 'h': 0}
    assert callable(backend.calls[0]['_pre_input_guard'])
    assert backend.closed == 1
    with pytest.raises(PageScopeError): scroll.scroll(3, 2)
    assert len(backend.calls) == 1


@pytest.mark.parametrize('amount', [0, 4, -4, True, 1.5])
def test_scroll_rejects_excessive_or_invalid_wheel_amount(amount):
    scroll, observer, backend, opened = observed_scroll_setup()
    with pytest.raises(PageScopeError): scroll.scroll(3, amount)
    assert not opened and not backend.calls


def test_scroll_will_not_use_stale_pointer_or_unknown_location():
    scroll, observer, backend, opened = observed_scroll_setup()
    observer.rows[0]['event']['target'] = 'a'
    with pytest.raises(PageScopeError): scroll.scroll(3, 1)
    assert not opened and not backend.calls


@pytest.mark.parametrize('fault', ['wrong_target', 'wrong_page', 'locked'])
def test_post_hello_guard_rechecks_real_page_frame_before_actual_input(fault):
    experiment, observer, backend, opened = setup()
    sent = []
    def execute_after_hello(request):
        if fault == 'wrong_target': observer.target = 'other'
        elif fault == 'wrong_page':
            original = observer.snapshot
            def wrong_page():
                snapshot = original()
                snapshot.image_bytes = fixture(9, nonce='other-page')[0]
                return snapshot
            observer.snapshot = wrong_page
        else:
            original = observer.snapshot
            def locked():
                snapshot = original(); snapshot.metadata['locked'] = 'LOCKED'; return snapshot
            observer.snapshot = locked
        request['_pre_input_guard']()
        sent.append(request)
        return {'state':'executed','ble_connected':True}
    backend.execute = execute_after_hello
    with pytest.raises(PageScopeError): experiment.move(1,0)
    assert not sent and backend.closed == 1


def test_post_action_progress_must_advance_beyond_final_pre_input_guard_frame():
    experiment, observer, backend, opened = setup()
    original = observer.snapshot
    frozen_counter = []
    def snapshot():
        value = original()
        if frozen_counter: value.image_bytes = fixture(frozen_counter[0])[0]
        return value
    observer.snapshot = snapshot
    def execute(request):
        request['_pre_input_guard']()
        frozen_counter.append(observer.seq)
        backend.calls.append(request)
        return {'state':'executed','ble_connected':True}
    backend.execute = execute
    with pytest.raises(PageScopeError): experiment.move(1,0)
    assert len(backend.calls) == 1 and backend.closed == 1


@pytest.mark.parametrize("phase", ["entry", "observe", "open", "hello"])
def test_shared_stop_prevents_action_at_every_pre_dispatch_boundary(phase):
    experiment, observer, backend, opened = setup()
    token = experiment.cancel_token
    if phase == "entry": token.set()
    if phase == "observe":
        original = observer.snapshot
        def stopped_observation():
            token.set()
            return original()
        observer.snapshot = stopped_observation
    if phase == "open":
        def stopped_open(): token.set(); return backend
        experiment.backend_opener = stopped_open
    if phase == "hello":
        def stopped_hello(request):
            token.set()
            request["_pre_input_guard"]()
            pytest.fail("guard must prevent action after hello")
        backend.execute = stopped_hello
    with pytest.raises(PageScopeError): experiment.move(1, 0)
    assert not backend.calls
    assert backend.closed == (1 if phase in {"open", "hello"} else 0)
    with pytest.raises(PageScopeError): experiment.move(1, 0)


def test_current_request_carries_same_latched_token_for_serial_cancellation():
    experiment, observer, backend, opened = setup()
    experiment.move(1, 0)
    assert backend.calls[0]["_cancel_event"] is experiment.cancel_token


@pytest.mark.parametrize("fault", ["moved_after_hello", "processing_expired", "stopped"])
def test_image_click_rechecks_pixels_and_freshness_before_action(fault):
    from phonelab.page_scope import PageImageLocatedClickExperiment
    movement, observer, backend, opened = setup()
    calls = []
    class ImageGuard:
        def verify(self, png):
            calls.append(png)
            if fault == "moved_after_hello": raise ValueError("pointer moved after hello")
            if fault == "processing_expired": observer.clock.sleep(2.1)
            if fault == "stopped": movement.cancel_token.set()
    click = PageImageLocatedClickExperiment(observer, target=movement.target,
        run_id=movement.run_id, nonce=movement.nonce, test_id=movement.test_id,
        backend_opener=movement.backend_opener, lease_factory=movement.lease_factory,
        cancel_token=movement.cancel_token, clock=movement.clock, sleep=movement.sleep,
        image_guard=ImageGuard())
    def execute(request):
        request["_pre_input_guard"]()
        backend.calls.append(request)
        return {"state":"executed", "ble_connected":True}
    backend.execute = execute
    with pytest.raises(PageScopeError): click.click()
    assert backend.calls == [] and backend.closed == 1


def test_image_click_has_separate_preparation_bound_and_one_second_hid_bound():
    from phonelab.page_scope import PageImageLocatedClickExperiment
    movement, observer, backend, opened = setup()
    checked = []
    class ImageGuard:
        def verify(self, png): checked.append(png)
    click = PageImageLocatedClickExperiment(observer, target=movement.target,
        run_id=movement.run_id, nonce=movement.nonce, test_id=movement.test_id,
        backend_opener=movement.backend_opener, lease_factory=movement.lease_factory,
        cancel_token=movement.cancel_token, clock=movement.clock, sleep=movement.sleep,
        image_guard=ImageGuard())
    def execute(request):
        request["_pre_input_guard"]()
        backend.calls.append(request)
        return {"state":"executed", "ble_connected":True}
    backend.execute = execute
    result = click.click()
    assert len(checked) == 1 and result["verified"] is False
    assert backend.calls[0]["_dispatch_ttl_seconds"] == 1
    assert backend.closed == 1


def test_image_guard_runs_once_after_serial_hello_and_before_requested_operation():
    from phonelab.page_scope import PageImageLocatedClickExperiment
    movement, observer, backend, opened = setup()
    order = []
    class ImageGuard:
        def verify(self, png):
            order.append("image_guard")
            return {"probe_id": "01", "count": 0}
    click = PageImageLocatedClickExperiment(observer, target=movement.target,
        run_id=movement.run_id, nonce=movement.nonce, test_id=movement.test_id,
        backend_opener=lambda: order.append("open") or backend,
        lease_factory=movement.lease_factory, cancel_token=movement.cancel_token,
        clock=movement.clock, sleep=movement.sleep, image_guard=ImageGuard(),
        expected_count=0, probe_id="01")
    def execute(request):
        order.append("hello")
        request["_pre_input_guard"]()
        order.append("requested_operation")
        backend.calls.append(request)
        return backend.result
    backend.execute = execute
    result = click.click()
    assert order == ["open", "hello", "image_guard", "requested_operation"]
    assert result["verified"] is False and len(backend.calls) == 1


def test_ordinary_movement_keeps_its_existing_early_and_final_hooks():
    experiment, observer, backend, opened = setup()
    calls = []
    experiment._before_dispatch = lambda snapshot=None: calls.append(snapshot)
    def execute(request):
        request["_pre_input_guard"]()
        backend.calls.append(request)
        return backend.result
    backend.execute = execute
    experiment.move(1, 0)
    assert len(calls) == 2 and calls[0] is not None and calls[1] is not None


def test_image_scroll_requires_unique_current_probe_guard_and_emits_only_wheel():
    from phonelab.page_scope import PageImageLocatedScrollExperiment
    movement, observer, backend, opened = setup()
    checked = []
    class Guard:
        def verify(self, png):
            checked.append(png)
            return {"probe_id": "10", "count": 2}
    scroll = PageImageLocatedScrollExperiment(observer, target=movement.target,
        run_id=movement.run_id, nonce=movement.nonce, test_id=movement.test_id,
        backend_opener=movement.backend_opener, lease_factory=movement.lease_factory,
        cancel_token=movement.cancel_token, clock=movement.clock, sleep=movement.sleep,
        image_guard=Guard(), probe_id="10", expected_count=2)
    def execute(request):
        request["_pre_input_guard"]()
        backend.calls.append(request)
        return {"state": "executed", "ble_connected": True}
    backend.execute = execute
    result = scroll.scroll(-2)
    assert result["verified"] is False and result["op"] == "scroll"
    assert len(checked) == 1
    assert backend.calls[0]["op"] == "scroll" and backend.calls[0]["args"] == {"v": -2, "h": 0}
    assert backend.calls[0]["_dispatch_ttl_seconds"] == 1
    assert backend.closed == 1


@pytest.mark.parametrize("vertical", [0, 4, -4, True])
def test_image_scroll_invalid_wheel_never_opens_backend(vertical):
    from phonelab.page_scope import PageImageLocatedScrollExperiment
    movement, observer, backend, opened = setup()
    class Guard:
        def verify(self, png): return {"probe_id": "10", "count": 0}
    scroll = PageImageLocatedScrollExperiment(observer, target=movement.target,
        run_id=movement.run_id, nonce=movement.nonce, test_id=movement.test_id,
        backend_opener=movement.backend_opener, lease_factory=movement.lease_factory,
        cancel_token=movement.cancel_token, clock=movement.clock, sleep=movement.sleep,
        image_guard=Guard(), probe_id="10", expected_count=0)
    with pytest.raises(PageScopeError): scroll.scroll(vertical)
    assert not opened and not backend.calls


@pytest.mark.parametrize("fault", ["wrong_target", "wrong_count", "stopped", "expired"])
def test_image_scroll_guard_must_stay_fresh_and_bound_to_expected_probe(fault):
    from phonelab.page_scope import PageImageLocatedScrollExperiment
    movement, observer, backend, opened = setup()
    checked = []
    class Guard:
        def verify(self, png):
            checked.append(png)
            if fault == "wrong_target": return {"probe_id": "11", "count": 2}
            if fault == "wrong_count": return {"probe_id": "10", "count": 3}
            if fault == "stopped": movement.cancel_token.set()
            if fault == "expired": observer.clock.sleep(2.1)
            return {"probe_id": "10", "count": 2}
    scroll = PageImageLocatedScrollExperiment(observer, target=movement.target,
        run_id=movement.run_id, nonce=movement.nonce, test_id=movement.test_id,
        backend_opener=movement.backend_opener, lease_factory=movement.lease_factory,
        cancel_token=movement.cancel_token, clock=movement.clock, sleep=movement.sleep,
        image_guard=Guard(), probe_id="10", expected_count=2)
    def execute(request):
        request["_pre_input_guard"]()
        backend.calls.append(request)
        return {"state": "executed", "ble_connected": True}
    backend.execute = execute
    with pytest.raises(PageScopeError): scroll.scroll(1)
    assert backend.calls == []


def test_image_scroll_stop_after_backend_open_cancels_final_guard_before_wire():
    from phonelab.page_scope import PageImageLocatedScrollExperiment
    movement, observer, backend, opened = setup()
    checks = []
    class Guard:
        def verify(self, png):
            checks.append(png)
            if len(checks) == 1:
                movement.cancel_token.set()
            return {"probe_id": "10", "count": 1}
    scroll = PageImageLocatedScrollExperiment(observer, target=movement.target,
        run_id=movement.run_id, nonce=movement.nonce, test_id=movement.test_id,
        backend_opener=movement.backend_opener, lease_factory=movement.lease_factory,
        cancel_token=movement.cancel_token, clock=movement.clock, sleep=movement.sleep,
        image_guard=Guard(), probe_id="10", expected_count=1)
    def execute(request):
        request["_pre_input_guard"]()
        backend.calls.append(request)
        return {"state": "executed", "ble_connected": True}
    backend.execute = execute
    with pytest.raises(PageScopeError):
        scroll.scroll(1)
    assert len(checks) == 1 and opened == [True]
    assert not backend.calls and backend.closed == 1
