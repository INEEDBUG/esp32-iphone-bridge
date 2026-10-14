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


@pytest.mark.parametrize('operation,arguments,expected',[
    ('type',{'text':'test123'},''),
    ('type-next',{'text':'Test 123','expected_text':'Test 12'},'Test 12'),
])
def test_text_candidate_selection_is_active_only_inside_final_input_guard(monkeypatch,tmp_path,operation,arguments,expected):
    import json
    import time
    from types import SimpleNamespace
    import phonelab.surface_experiments as module
    import phonelab.local_english_ocr as local_ocr
    monkeypatch.setattr(local_ocr,'prepare_text_ocr',lambda:None)

    snapshot=SimpleNamespace(image_bytes=b'frame',metadata={'received_monotonic':time.monotonic()})
    class Observer:
        def visible_run(self,**kwargs):return {'run_id':'run-1','nonce':'nonce-1'}
        def snapshot(self):return snapshot
        def events(self,run_id):return {'run':{'run_id':run_id,'nonce':'nonce-1','test_id':module.TEST_ID},'events':[]}
    observer=Observer(); selection_calls=[];request_deadlines=[]
    def select(reader,guard,**kwargs):
        selection_calls.append(kwargs)
        return reader()
    monkeypatch.setattr(module,'select_visible_caret_snapshot',select,raising=False)
    monkeypatch.setattr(module,'_private_root',lambda path:path)
    monkeypatch.setattr(module,'_load_control_token',lambda private:'control')
    evidence=tmp_path/'evidence';evidence.mkdir()
    monkeypatch.setattr(module,'_new_evidence_dir',lambda private:evidence)
    monkeypatch.setattr(module,'_save_snapshot',lambda *args,**kwargs:None)
    monkeypatch.setattr(module,'_safe_events',lambda *args,**kwargs:{'events':[]})
    monkeypatch.setattr(module,'_validate_snapshot',lambda value,**kwargs:value)
    monkeypatch.setattr(module,'read_target_binding',lambda data:('serial','target'))
    monkeypatch.setattr(module,'LiveObservationClient',lambda *args,**kwargs:observer)
    class Wire:
        def write(self,raw):return None
    class Backend:
        def __init__(self):self.wire=Wire()
        def execute(self,request):
            request['_pre_input_guard']()
            self.wire.write(json.dumps({'op':'type'}))
            return {'state':'executed','ble_connected':True,'mouse_ready':True}
        def close(self):return None
    monkeypatch.setattr(module.runtime,'open_approved',lambda **kwargs:Backend())
    class Scope:
        def __init__(self,observer,**kwargs):
            self.observer=observer;self.guard=kwargs['image_guard'];self.backend_opener=kwargs['backend_opener']
        def type_phrase(self,text):
            assert self.guard.expected_text==expected
            before=self.observer.snapshot()
            backend=self.backend_opener()
            deadline=time.monotonic()+5;request_deadlines.append(deadline)
            request={'id':'request','session':'session','op':'type','deadline':deadline,
                     '_pre_input_guard':lambda:self.observer.snapshot()}
            backend.execute(request);backend.close()
            after=self.observer.snapshot()
            return {'before':before,'after':after}
        def type_next_character(self,text):return self.type_phrase(text)
    monkeypatch.setattr(module,'PageImageLocatedTextExperiment',Scope)
    token=SimpleNamespace(is_set=lambda:False)
    result=module.run_surface_experiment(operation,arguments,data_root=tmp_path,
        private_root=tmp_path/'private',stop_token=token)
    assert result['state']=='executed',result
    assert len(selection_calls)==1
    assert selection_calls[0]['request_deadline']==request_deadlines[0]
