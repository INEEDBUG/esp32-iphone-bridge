"""Safety behavior of the one-action, synthetic-page-only movement experiment."""
from contextlib import nullcontext
from types import SimpleNamespace
import threading

import pytest

from phonelab.page_scope import (PageScopeError, PageMovementExperiment,
                                PageConfirmedClickExperiment, OnsitePointerConfirmation)
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
            if fault == "moved_after_hello" and len(calls) == 2: raise ValueError("pointer moved")
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
    assert len(checked) == 2 and result["verified"] is False
    assert backend.calls[0]["_dispatch_ttl_seconds"] == 1
    assert backend.closed == 1
