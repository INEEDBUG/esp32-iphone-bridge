import pytest


@pytest.mark.parametrize('phrase,prefix', [('Test 123','Test 12'), ('Test 123','Test'), ('test123!@#','test123!')])
def test_text_step_request_requires_one_fixed_phrase_and_its_incomplete_prefix(phrase,prefix):
    from phonelab.surface_experiments import validate_surface_request
    args={'text':phrase,'expected_text':prefix}
    assert validate_surface_request('type-next',args)==args


@pytest.mark.parametrize('args', [
    {'text':'private','expected_text':''},
    {'text':'Test 123','expected_text':'TEST'},
    {'text':'Test 123','expected_text':'Test 123'},
    {'text':'Test 123','expected_text':True},
    {'text':'Test 123','expected_text':'','extra':1},
])
def test_text_step_rejects_ambiguous_or_complete_requests_before_device_access(args):
    from phonelab.surface_experiments import validate_surface_request
    with pytest.raises(ValueError):validate_surface_request('type-next',args)


def test_text_step_cli_remains_preview_only(monkeypatch,capsys):
    import phonelab.surface_experiments as module
    monkeypatch.setattr(module,'run_surface_experiment',lambda *a,**k:pytest.fail('device access'))
    assert module.main(['type-next','--text','Test 123','--expected-text','Test 12'])==0
    assert 'PREVIEW_ONLY' in capsys.readouterr().out
