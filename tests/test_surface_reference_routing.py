import pytest

from phonelab.surface_experiments import run_surface_experiment


@pytest.mark.parametrize('operation,arguments',[
    ('backspace',{'expected_text':'test1'}),('type',{'text':'test123'}),
])
def test_unverified_text_reference_is_rejected_before_device_access(operation,arguments,monkeypatch):
    import phonelab.surface_experiments as module
    monkeypatch.setattr(module,'read_target_binding',lambda *args:pytest.fail('device access before reference validation'))
    with pytest.raises(ValueError,match='verified prefix reference'):
        run_surface_experiment(operation,arguments,text_reference=object())
