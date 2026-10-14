import pytest


def test_surface_requests_have_narrow_noninterchangeable_arguments():
    from phonelab.surface_experiments import validate_surface_request
    assert validate_surface_request('focus-text',{})=={}
    assert validate_surface_request('type',{'text':'test123'})=={'text':'test123'}
    assert validate_surface_request('backspace',{'expected_text':'test12'})=={'expected_text':'test12'}
    assert validate_surface_request('scroll',{'steps':3,'vertical':-1})=={'steps':3,'vertical':-1}
    for op,args in [('type',{'text':'private message'}),('type',{'text':'test123','key':'enter'}),
                    ('backspace',{'expected_text':''}),('scroll',{'steps':True,'vertical':1}),
                    ('drag',{'dx':129,'dy':0}),('home',{}),('focus-text',{'text':'test123'})]:
        with pytest.raises(ValueError):validate_surface_request(op,args)


def test_default_cli_is_preview_and_never_reads_device_or_opens_uart(monkeypatch,capsys):
    import phonelab.surface_experiments as module
    monkeypatch.setattr(module,'run_surface_experiment',lambda *a,**k:pytest.fail('unexpected execution'))
    assert module.main(['type','--text','test123'])==0
    assert 'PREVIEW_ONLY' in capsys.readouterr().out
