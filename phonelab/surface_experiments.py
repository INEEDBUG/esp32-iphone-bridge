"""Explicit synthetic-field/native-wheel/pointer-drag trials. Default: preview only."""
import argparse
import json
from pathlib import Path
import time

from . import deploy,runtime
from .iphone import read_target_binding
from .live_observation import LiveObservationClient,BASE_URL
from .page_experiments import (_load_control_token,_private_root,_validate_snapshot,
    _new_evidence_dir,_save_snapshot,_safe_events,ROOT,TEST_ID,SEED)
from .page_scope import (PageImageLocatedClickExperiment,PageImageLocatedTextExperiment,
    PageScopeError,windows_input_lease,_synthetic_text_prefix,_SYNTHETIC_TEXT_PHRASES)
from .image_text_guard import ImageTextFieldGuard,select_visible_caret_snapshot
from .surface_sequences import SurfacePointerGuard,SurfaceScrollSequence,SurfaceDragGesture
from .stop_signal import StopStore


def validate_surface_request(operation,args):
    valid=False
    if operation=='focus-text':valid=args=={}
    elif operation=='type':valid=set(args)=={'text'} and args['text'] in _SYNTHETIC_TEXT_PHRASES
    elif operation=='backspace':valid=set(args)=={'expected_text'} and bool(args['expected_text']) and _synthetic_text_prefix(args['expected_text'])
    elif operation=='scroll':valid=(set(args)=={'steps','vertical'} and type(args['steps']) is int and 1<=args['steps']<=32 and type(args['vertical']) is int and args['vertical'] in (-1,1))
    elif operation=='drag':valid=(set(args)=={'dx','dy'} and type(args['dx']) is int and 1<=args['dx']<=128 and type(args['dy']) is int and args['dy']==0)
    if not valid:raise ValueError('unsupported synthetic surface request')
    return dict(args)


def run_surface_experiment(operation,arguments,*,data_root=deploy.DATA,private_root=None,stop_token=None,text_reference=None):
    arguments=validate_surface_request(operation,arguments)
    if text_reference is not None:
        from .prefix_image_reference import VerifiedPrefixImage
        if (not isinstance(text_reference,VerifiedPrefixImage) or operation!='backspace'
                or not text_reference.full_text.startswith(arguments['expected_text'])):
            raise ValueError('backspace requires a verified prefix reference for this phrase')
    data=Path(data_root);private=_private_root(Path(private_root) if private_root else data/'private')
    token=stop_token if stop_token is not None else StopStore(private).capture()
    evidence=None;observer=None;scope=None;run_id=None;target=None;nonce=None
    input_writes=[];observed=[]
    select_text_candidate=False;text_guard=None;selection_request_deadline=None
    started=time.monotonic()
    def trace(phase,**fields):
        if evidence is not None:
            with (evidence/'phases.jsonl').open('a',encoding='utf-8') as handle:
                handle.write(json.dumps({'elapsed':round(time.monotonic()-started,4),'phase':phase,**fields})+'\n')
    try:
        if token.is_set():raise ValueError('stopped generation')
        if operation in ('type','backspace'):
            from .local_english_ocr import prepare_text_ocr
            prepare_text_ocr()  # Startup is outside the original-frame validity window.
        _serial,target=read_target_binding(data)
        observer=LiveObservationClient(BASE_URL,_load_control_token(private),target)
        context=observer.visible_run(test_id=TEST_ID,seed=SEED)
        run_id,nonce=context['run_id'],context['nonce']
        snapshot=_validate_snapshot(observer.snapshot(),target=target,nonce=nonce,clock=time.monotonic)
        evidence=_new_evidence_dir(private)
        _save_snapshot(evidence,'page-association',snapshot,target=target,nonce=nonce,clock=time.monotonic)
        baseline=_safe_events(observer,run_id)
        (evidence/'baseline-events.json').write_text(json.dumps(baseline),encoding='utf-8')
        inner=observer
        def check_selection_cancelled():
            if token.is_set():raise PageScopeError('stopped generation during caret selection')
        class Recording:
            def events(self,*args):return inner.events(*args)
            def _read_snapshot(self):
                if token.is_set():raise PageScopeError('stopped generation during image selection')
                value=inner.snapshot()
                _validate_snapshot(value,target=target,nonce=nonce,clock=time.monotonic)
                run=inner.events(run_id).get('run')
                if not isinstance(run,dict) or any(
                        run.get(key)!=expected for key,expected in
                        (('run_id',run_id),('nonce',nonce),('test_id',TEST_ID))):
                    raise PageScopeError('page session changed during image selection')
                observed.append(value)
                return value
            def snapshot(self):
                if select_text_candidate:
                    return select_visible_caret_snapshot(self._read_snapshot,text_guard,
                        clock=time.monotonic,sleep=time.sleep,
                        check_cancelled=check_selection_cancelled,
                        request_deadline=selection_request_deadline)
                return self._read_snapshot()
        def open_backend():
            backend=runtime.open_approved(data=data,private=private,image_root=ROOT,
                epoch_reader=runtime.read_windows_connection_epoch,stop_token=token)
            original_write=backend.wire.write
            def write(raw):
                packet=json.loads(raw);op=packet.get('op')
                if op not in {'hello','ping','stop','move','click','press','release','scroll','type','key'}:
                    raise ValueError('unexpected wire operation')
                if op not in {'hello','ping','stop'}:input_writes.append(op)
                trace('wire_write',op=op,bytes=len(raw))
                return original_write(raw)
            backend.wire.write=write
            original_execute=backend.execute
            def execute(request):
                nonlocal select_text_candidate,selection_request_deadline
                check=request['_pre_input_guard']
                def guarded():
                    nonlocal select_text_candidate,selection_request_deadline
                    trace('guard_begin',op=request['op'])
                    select_text_candidate=request['op'] in ('type','key') and text_guard is not None
                    selection_request_deadline=request['deadline']
                    try:
                        result=check();trace('guard_pass',op=request['op']);return result
                    finally:
                        select_text_candidate=False;selection_request_deadline=None
                result=original_execute({**request,'_pre_input_guard':guarded})
                trace('ack',op=request['op'],state=result.get('state'),
                      mouse_ready=result.get('mouse_ready'),ble_connected=result.get('ble_connected'))
                return result
            backend.execute=execute
            return backend
        common=dict(target=target,run_id=run_id,nonce=nonce,test_id=TEST_ID,backend_opener=open_backend,
            lease_factory=lambda:windows_input_lease(private/'input-owner.lock'),cancel_token=token)
        recording=Recording()
        if operation=='focus-text':
            scope=PageImageLocatedClickExperiment(recording,image_guard=SurfacePointerGuard(nonce,'text'),**common)
            result=scope.click()
        elif operation in ('type','backspace'):
            expected='' if operation=='type' else arguments['expected_text']
            text_guard=ImageTextFieldGuard(nonce,expected,
                ocr_reader=text_reference.ocr_reader if text_reference is not None else None)
            scope=PageImageLocatedTextExperiment(recording,image_guard=text_guard,expected_text=expected,**common)
            result=scope.type_phrase(arguments['text']) if operation=='type' else scope.backspace()
        elif operation=='scroll':
            scope=SurfaceScrollSequence(recording,image_guard=SurfacePointerGuard(nonce,'native-scroll'),**common)
            result=scope.scroll(**arguments)
        else:
            scope=SurfaceDragGesture(recording,image_guard=SurfacePointerGuard(nonce,'drag-source'),**common)
            result=scope.drag(**arguments)
        for name in ('before','after'):
            _save_snapshot(evidence,name,result[name],target=target,nonce=nonce,clock=time.monotonic)
        state='executed'
        error_type=None
    except BaseException as error:
        state='UNKNOWN_OR_NOT_SENT' if input_writes else 'NOT_SENT'
        error_type=type(error).__name__
        trace('stopped',error_type=error_type,input_writes=len(input_writes))
        if evidence is not None and observed:
            _save_snapshot(evidence,'after',observed[-1],target=target,nonce=nonce,clock=time.monotonic)
    if evidence is None:
        return {'state':state,'verified':False,'error_type':error_type,'input_writes':0}
    for index,step in enumerate(getattr(scope,'evidence_steps',())):
        directory=evidence/'guarded-steps'/str(index+1).zfill(2)
        directory.mkdir(parents=True,exist_ok=True)
        _save_snapshot(directory,'before',step['snapshot'],target=target,nonce=nonce,clock=time.monotonic)
    (evidence/'events.json').write_text(json.dumps(_safe_events(observer,run_id)),encoding='utf-8')
    receipt={'state':state,'verified':False,'operation':operation,'arguments':arguments,
             'input_writes':input_writes,'error_type':error_type,'lock_state':'UNKNOWN',
             'interaction_scope':'current_synthetic_page_only'}
    (evidence/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
    return {'state':state,'verified':False,'evidence_path':str(evidence),
            'input_writes':len(input_writes),'error_type':error_type}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    sub=parser.add_subparsers(dest='operation',required=True)
    sub.add_parser('focus-text')
    sub.add_parser('type').add_argument('--text',choices=_SYNTHETIC_TEXT_PHRASES,required=True)
    sub.add_parser('backspace').add_argument('--expected-text',required=True)
    scroll=sub.add_parser('scroll');scroll.add_argument('--steps',type=int,required=True);scroll.add_argument('--vertical',type=int,choices=(-1,1),required=True)
    drag=sub.add_parser('drag');drag.add_argument('--dx',type=int,required=True);drag.add_argument('--dy',type=int,default=0)
    values=vars(parser.parse_args(argv));execute=values.pop('execute');operation=values.pop('operation')
    arguments=validate_surface_request(operation,values)
    result=run_surface_experiment(operation,arguments) if execute else {'state':'PREVIEW_ONLY','operation':operation,'arguments':arguments}
    print(json.dumps(result,sort_keys=True))
    return 0 if result['state'] in ('PREVIEW_ONLY','executed') else 1


if __name__=='__main__':raise SystemExit(main())
