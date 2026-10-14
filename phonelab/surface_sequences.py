"""Finite native wheel/drag transactions; every write has a fresh full image guard."""
import uuid

from .page_scope import _OnePageAction,PageScopeError
from .visual_marker import VisualMarkerTracker,decode_visual_marker
from .image_surface import find_surface
from .synthetic_vision import detect_pointer


class SurfacePointerGuard:
    def __init__(self,nonce,role):
        if role not in ('text','native-scroll','drag-source'):
            raise ValueError('unsupported pointer surface')
        self.nonce,self.role=nonce,role
        self.anchors=None

    def verify(self,png,stage='click'):
        surface=find_surface(png,self.nonce,self.role)
        marker=decode_visual_marker(png,self.nonce)
        pointer=detect_pointer(png,verified_marker=marker)
        if pointer is None:
            raise ValueError('unique visible pointer required')
        bounds=surface['bounds']
        if self.role=='drag-source':
            drop=find_surface(png,self.nonce,'drop-target')['bounds']
            current=(bounds,drop)
            if self.anchors is None:self.anchors=current
            if current!=self.anchors:
                raise ValueError('drag surface geometry changed')
            if stage=='release':bounds=drop
            elif stage not in ('press','move'):
                raise ValueError('invalid drag stage')
        x,y=pointer.center_x,pointer.center_y
        l,t,r,b=bounds
        if not l+12<x<r-12 or not t+12<y<b-12:
            raise ValueError('pointer outside selected synthetic surface')
        return {'role':self.role,'bounds':surface['bounds'],'pointer':(x,y)}


class _SurfaceSequence(_OnePageAction):
    observation_max_age=.25

    def __init__(self,*args,image_guard,**kwargs):
        super().__init__(*args,**kwargs)
        self.image_guard=image_guard
        self.evidence_steps=[]

    def _run(self,commands,*,maximum_seconds,held=False):
        if not self._action.acquire(blocking=False):
            raise PageScopeError('an action is already running')
        backend=None
        try:
            if self._consumed:raise PageScopeError('one-shot consumed; never replay')
            self._consumed=True
            with self.lease_factory():
                self._check_cancelled()
                tracker=VisualMarkerTracker(self.nonce)
                first,_=self._observe(tracker,0)
                self.sleep(.7)
                before,state=self._observe(tracker,first.metadata['sequence'])
                if state['state']!='progress_observed':
                    raise PageScopeError('synthetic page progression unavailable')
                backend=self.backend_opener()
                self._check_cancelled()
                session=uuid.uuid4().hex
                overall_end=self.clock()+maximum_seconds
                held_end=None
                previous=before.metadata['sequence']
                requests=[]
                initial_frame=None
                for operation,arguments in commands:
                    self._check_cancelled()
                    step_end=min(overall_end,self.clock()+5)
                    if held_end is not None:step_end=min(step_end,held_end)
                    if self.clock()>=step_end:raise PageScopeError('finite sequence expired')
                    def guard(op=operation,end=step_end):
                        nonlocal before,previous,initial_frame
                        self._check_cancelled()
                        before,_=self._observe(tracker,previous)
                        previous=before.metadata['sequence']
                        self.image_guard.verify(before.image_bytes,op)
                        self._check_cancelled()
                        expiry=min(end,before.metadata['received_monotonic']+2)
                        if self.clock()>=expiry:
                            raise PageScopeError('sequence image or held deadline expired')
                        if initial_frame is None:initial_frame=before
                        self.evidence_steps.append({'operation':op,'snapshot':before})
                        return {'valid_before_monotonic':expiry}
                    request={'id':uuid.uuid4().hex,'session':session,
                             'owner':'onsite-synthetic-surface','op':operation,'args':arguments,
                             'deadline':step_end,'_pre_input_guard':guard,
                             '_dispatch_ttl_seconds':1,'_cancel_event':self.cancel_token}
                    # The held limit starts BEFORE press is prepared/sent, never at its ACK.
                    if held and operation=='press':
                        held_end=self.clock()+3
                        request['deadline']=min(request['deadline'],held_end)
                    reply=backend.execute(request)
                    self._check_cancelled()
                    if (not isinstance(reply,dict) or reply.get('state')!='executed'
                            or reply.get('ble_connected') is not True or reply.get('mouse_ready') is not True):
                        raise PageScopeError('sequence outcome unknown or rejected; never replay')
                    if self.clock()>=request['deadline']:
                        raise PageScopeError('sequence deadline expired; never replay')
                    requests.append(request['id'])
                backend.close();backend=None
                self.sleep(.7)
                after,state=self._observe(tracker,previous)
                if state['state']!='progress_observed':
                    raise PageScopeError('post-sequence observation unavailable; never replay')
                return {'state':'executed','verified':False,'lock_state':'UNKNOWN',
                        'interaction_scope':'current_synthetic_page_only','before':initial_frame,
                        'after':after,'request_ids':requests}
        except PageScopeError:raise
        except Exception:
            raise PageScopeError('sequence stopped; outcome may be unknown; never replay') from None
        finally:
            if backend is not None:
                try:backend.close()  # STOP is permitted even when a held gesture fails its guard.
                except Exception:pass
            self._action.release()


class SurfaceScrollSequence(_SurfaceSequence):
    def scroll(self,*,steps,vertical):
        if type(steps) is not int or not 1<=steps<=32 or type(vertical) is not int or vertical not in (-1,1):
            raise ValueError('one to 32 unit wheels in one direction required')
        # Wheel reports alone need not keep the AssistiveTouch pointer visible.
        # Alternate a bounded unheld unit move under its own fresh surface guard;
        # net travel is at most one unit and no held input is introduced.
        commands=[]
        for index in range(steps):
            commands.extend([('move',{'dx':0,'dy':1 if index%2==0 else -1}),
                             ('scroll',{'v':vertical,'h':0})])
        return self._run(commands,maximum_seconds=120)


class SurfaceDragGesture(_SurfaceSequence):
    def drag(self,*,dx,dy):
        if (type(dx) is not int or type(dy) is not int or not 1<=dx<=128 or dy!=0):
            raise ValueError('one bounded horizontal synthetic drag required')
        # STOP sends zero reports but the deployed core's held-key/cache state
        # clears on its next update. A newly guarded unheld unit move performs
        # that update before any press; it cannot replay old cleared axes.
        return self._run([('move',{'dx':0,'dy':1}),('press',{}),
            ('move',{'dx':dx,'dy':0}),('release',{})],maximum_seconds=5,held=True)
