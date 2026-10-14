from contextlib import nullcontext
import pytest
from test_page_scope import Clock,Observer
from phonelab.stop_signal import StopStore


class Backend:
    def __init__(self): self.calls=[]; self.closed=0; self.reject_at=None
    def execute(self,request):
        request['_pre_input_guard']()
        self.calls.append(request)
        return {'state':'rejected' if len(self.calls)==self.reject_at else 'executed',
                'ble_connected':True,'mouse_ready':True}
    def close(self):self.closed+=1


class Guard:
    def __init__(self):self.stages=[];self.fail=None
    def verify(self,png,stage):
        self.stages.append(stage)
        if stage==self.fail:raise ValueError('changed image geometry')


def make(tmp_path,kind):
    from phonelab.surface_sequences import SurfaceScrollSequence,SurfaceDragGesture
    clock=Clock();observer=Observer(clock);backend=Backend();guard=Guard();opened=[]
    def open_backend():opened.append(True);return backend
    cls=SurfaceScrollSequence if kind=='scroll' else SurfaceDragGesture
    item=cls(observer,target='test-target',run_id='run1',nonce='hello',test_id='ble-round-01',
        backend_opener=open_backend,lease_factory=nullcontext,cancel_token=StopStore(tmp_path).capture(),
        clock=clock,sleep=clock.sleep,image_guard=guard)
    return item,backend,guard,clock,opened


def test_scroll_has_one_owner_session_and_a_full_guard_before_every_write(tmp_path):
    item,backend,guard,_,opened=make(tmp_path,'scroll')
    result=item.scroll(steps=3,vertical=1)
    assert result['state']=='executed' and result['verified'] is False
    assert len(opened)==1 and backend.closed==1
    assert [r['op'] for r in backend.calls]==['move','scroll']*3
    assert len({r['session'] for r in backend.calls})==1
    assert len({r['id'] for r in backend.calls})==6
    assert guard.stages==['move','scroll']*3
    assert result['before'].metadata['sequence']==3
    assert len(item.evidence_steps)==6
    assert [r['args'] for r in backend.calls if r['op']=='scroll']==[{'v':1,'h':0}]*3
    assert [r['args'] for r in backend.calls if r['op']=='move']==[{'dx':0,'dy':1},{'dx':0,'dy':-1},{'dx':0,'dy':1}]
    assert all(r['_dispatch_ttl_seconds']==1 for r in backend.calls)
    with pytest.raises(Exception):item.scroll(steps=3,vertical=1)
    assert len(backend.calls)==6


@pytest.mark.parametrize('steps,vertical',[(0,1),(33,1),(True,1),(2,True),(2,2)])
def test_scroll_invalid_parameters_never_open_serial(tmp_path,steps,vertical):
    item,backend,_,_,opened=make(tmp_path,'scroll')
    with pytest.raises(ValueError):item.scroll(steps=steps,vertical=vertical)
    assert not opened and not backend.calls


def test_drag_press_move_release_share_session_and_each_recheck_geometry(tmp_path):
    item,backend,guard,_,_=make(tmp_path,'drag')
    item.drag(dx=80,dy=0)
    assert [r['op'] for r in backend.calls]==['move','press','move','release']
    assert guard.stages==['move','press','move','release']
    assert len({r['session'] for r in backend.calls})==1 and backend.closed==1


def test_guard_failure_after_press_closes_without_sending_move_or_replaying(tmp_path):
    item,backend,guard,_,_=make(tmp_path,'drag');original=guard.verify
    def fail_held_move(png,stage):
        original(png,stage)
        if stage=='move' and 'press' in guard.stages:raise ValueError('changed held geometry')
    guard.verify=fail_held_move
    with pytest.raises(Exception):item.drag(dx=80,dy=0)
    assert [r['op'] for r in backend.calls]==['move','press'] and backend.closed==1
    with pytest.raises(Exception):item.drag(dx=80,dy=0)
    assert len(backend.calls)==2


def test_rejected_scroll_stops_remaining_steps_and_closes(tmp_path):
    item,backend,_,_,_=make(tmp_path,'scroll');backend.reject_at=2
    with pytest.raises(Exception):item.scroll(steps=4,vertical=-1)
    assert len(backend.calls)==2 and backend.closed==1


def test_drag_synchronizes_unheld_core_before_press_with_a_fresh_guard(tmp_path):
    item,backend,guard,_,_=make(tmp_path,'drag')
    item.drag(dx=80,dy=0)
    assert backend.calls[0]['op']=='move' and backend.calls[0]['args']=={'dx':0,'dy':1}
    assert [r['op'] for r in backend.calls]==['move','press','move','release']
    assert guard.stages==['move','press','move','release']
    assert len(item.evidence_steps)==4
    assert len({r['session'] for r in backend.calls})==1


def test_cancellation_after_unheld_preparation_never_sends_press(tmp_path):
    item,backend,_,_,_=make(tmp_path,'drag');execute=backend.execute
    def cancel_after_first(request):
        reply=execute(request);StopStore(tmp_path).publish_stop();return reply
    backend.execute=cancel_after_first
    with pytest.raises(Exception):item.drag(dx=80,dy=0)
    assert [r['op'] for r in backend.calls]==['move'] and backend.closed==1


def test_preparation_does_not_renew_or_consume_the_held_timer(tmp_path):
    item,backend,guard,clock,_=make(tmp_path,'drag');original=guard.verify
    def timed(png,stage):
        original(png,stage)
        clock.now += 1.5 if len(guard.stages)==1 else 1.3 if stage=='release' else .9
    guard.verify=timed
    with pytest.raises(Exception):item.drag(dx=80,dy=0)
    # All individual images are younger than2s, overall is under5s, but the
    # three-second held interval expires before release dispatch.
    assert [r['op'] for r in backend.calls]==['move','press','move']


def test_held_drag_deadline_never_renews_during_validation(tmp_path):
    item,backend,guard,clock,_=make(tmp_path,'drag')
    original=guard.verify
    def slow(png,stage):
        original(png,stage)
        if stage=='move' and 'press' in guard.stages:clock.now+=2.1
    guard.verify=slow
    with pytest.raises(Exception):item.drag(dx=80,dy=0)
    assert [r['op'] for r in backend.calls]==['move','press'] and backend.closed==1


def test_scroll_cancel_after_wake_never_sends_wheel(tmp_path):
    item,backend,_,_,_=make(tmp_path,'scroll');execute=backend.execute
    def cancel_after_first(request):
        reply=execute(request);StopStore(tmp_path).publish_stop();return reply
    backend.execute=cancel_after_first
    with pytest.raises(Exception):item.scroll(steps=4,vertical=1)
    assert [r['op'] for r in backend.calls]==['move'] and backend.closed==1
