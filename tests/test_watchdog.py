import time
from test_control import ready


def test_frozen_observation_releases_held_state_without_new_command():
    c, b = ready()
    c.frame.received_monotonic = time.monotonic()-10
    result = c.watchdog()
    assert result['state'] == 'stopped'
    assert c.get_status()['armed'] is False
    assert c.calibration is None


def test_unknown_lock_state_blocks_arming():
    c, b = ready()
    c.frame.locked = None
    c.watchdog()
    assert c.armed is False
