import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from measure_usb_capture import validate_status


def snapshot():
    return {'armed': False, 'ACTIVE_INPUT_BACKEND': 'none', 'screen_capture': {
        'target': 'test-target', 'fresh': True, 'source': 'USB_SCREENSHOT',
        'source_time': None, 'locked': 'UNKNOWN', 'sequence': 3,
        'received_monotonic': 10.0, 'width': 828, 'height': 1792, 'orientation': 'portrait'}}


def test_measurement_accepts_only_current_capture_and_no_input():
    assert validate_status(snapshot(), 'test-target', 10.5, 2) == {'sequence': 3, 'age_seconds': .5}
    state = snapshot(); state['armed'] = True
    with pytest.raises(ValueError): validate_status(state, 'test-target', 10.5)


@pytest.mark.parametrize('now,previous', [(12.1, 0), (9.9, 0), (10.5, 4)])
def test_measurement_rejects_stale_future_or_reset_sequence(now, previous):
    with pytest.raises(ValueError): validate_status(snapshot(), 'test-target', now, previous)


def test_measurement_refuses_another_phone_or_rotated_frame():
    with pytest.raises(ValueError): validate_status(snapshot(), 'other-target', 10.5)
    state = snapshot(); state['screen_capture']['orientation'] = 'landscape'
    with pytest.raises(ValueError): validate_status(state, 'test-target', 10.5)
