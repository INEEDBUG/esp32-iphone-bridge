"""JavaScript unit checks only; these are not phone or trusted-input evidence."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


def test_interaction_surfaces_are_reachable_and_native_caret_is_visible():
    html = (Path(__file__).resolve().parents[1] / 'phonelab/static/lab.html').read_text(encoding='utf-8')
    assert html.index('id="test-text"') < html.index('id="probe-grid"')
    assert html.index('id="drag-source"') < html.index('id="probe-grid"')
    assert 'caret-color: #e11d48' in html
    assert 'id="native-scroll"' in html
    assert 'draggable="false"' in html
    assert 'event.isTrusted' in html
    assert '.value =' not in html and '.value=' not in html


def test_visible_probe_counts_require_trusted_events_and_remain_per_target():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is required for isolated page logic checks')
    page = Path(__file__).resolve().parents[1] / 'phonelab/static/lab.html'
    script = r'''
const fs = require('fs'); const vm = require('vm');
const handlers = {}; const nodes = new Map();
class Element {
  constructor(id = '', probe = '') { this.id=id; this.dataset=probe?{probe}:{};
    this.style={}; this.tagName='BUTTON'; this.textContent=''; this.value=''; this.width=40; this.height=40; }
  getContext() { return { imageSmoothingEnabled:true, createImageData(w,h) { return {data:new Uint8ClampedArray(w*h*4)}; }, putImageData() {} }; }
  closest(selector) { return selector==='.probe' && this.dataset.probe ? this : null; }
  addEventListener() {}
}
const probes = Array.from({length:25},(_,i)=>new Element('',String(i+1).padStart(2,'0')));
const document = {
  getElementById(id) { if(!nodes.has(id)) nodes.set(id,new Element(id)); return nodes.get(id); },
  querySelectorAll() { return probes; },
  addEventListener(name,fn) { handlers[name]=fn; }
};
const context = {Element, document, window:{scrollX:0,scrollY:0,addEventListener(){}},
  setInterval(){}, fetch(){return Promise.resolve({ok:true})}, performance:{now(){return 0}}};
const html=fs.readFileSync(process.argv[1],'utf8');
const code=html.match(/<script>([\s\S]*?)<\/script>/)[1].replace('__PHONE_LAB_CONFIG__',
  JSON.stringify({testId:'unit-only',seed:'42',nonce:'synthetic-nonce',runToken:'synthetic-token'}));
vm.runInNewContext(code,context);
function click(index,trusted) { handlers.click({type:'click',target:probes[index],
  isTrusted:trusted,timeStamp:1,clientX:10,clientY:10,button:0}); }
click(0,true); click(0,true); click(1,true);
const trusted=document.getElementById('probe-summary').textContent;
click(0,false);
process.stdout.write(JSON.stringify({trusted,afterUntrusted:document.getElementById('probe-summary').textContent,
  first:probes[0].textContent,second:probes[1].textContent}));
'''
    result = subprocess.run([node, '-e', script, str(page)], capture_output=True,
                            text=True, timeout=10, check=True)
    observed = json.loads(result.stdout)
    assert observed['trusted'] == 'Browser-reported probe clicks: 3 | Last: 02'
    assert observed['afterUntrusted'] == observed['trusted']
    assert observed['first'] == 'Probe 01 (2)'
    assert observed['second'] == 'Probe 02 (1)'


def test_pointermove_telemetry_is_common_allowlist_shape_and_throttled():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is required for isolated page logic checks')
    page = Path(__file__).resolve().parents[1] / 'phonelab/static/lab.html'
    script = r'''
const fs=require('fs'),vm=require('vm'); const handlers={},nodes=new Map(),sent=[];
let now=0,nextTimer=1; const timers=new Map();
class Element {
 constructor(id=''){this.id=id;this.dataset={};this.style={};this.tagName='DIV';this.textContent='';
  this.value='';this.width=40;this.height=40;}
 getContext(){return {imageSmoothingEnabled:true,createImageData(w,h){return {data:new Uint8ClampedArray(w*h*4)};},putImageData(){}};}
 closest(){return null;} addEventListener(){}
}
const probes=Array.from({length:25},(_,i)=>{const e=new Element();e.dataset={probe:String(i+1).padStart(2,'0')};e.tagName='BUTTON';return e;});
const document={getElementById(id){if(!nodes.has(id))nodes.set(id,new Element(id));return nodes.get(id);},
 querySelectorAll(){return probes;},addEventListener(name,fn){handlers[name]=fn;}};
const context={Element,document,window:{scrollX:0,scrollY:0,addEventListener(){}},setInterval(){},
 performance:{now(){return now;}},setTimeout(fn,delay){const id=nextTimer++;timers.set(id,{at:now+delay,fn});return id;},
 clearTimeout(id){timers.delete(id);},fetch(_url,request){sent.push(JSON.parse(request.body));return Promise.resolve({ok:true});}};
const html=fs.readFileSync(process.argv[1],'utf8');
const code=html.match(/<script>([\s\S]*?)<\/script>/)[1].replace('__PHONE_LAB_CONFIG__',
 JSON.stringify({testId:'calibration-only',seed:'7',nonce:'synthetic',runToken:'synthetic'}));
vm.runInNewContext(code,context);
const target=new Element('drag-source');
function runTimers(){for(const [id,timer] of [...timers])if(timer.at<=now){timers.delete(id);timer.fn();}}
for(const at of [0,50,199,200,399,400]) {now=at;handlers.pointermove({type:'pointermove',target,isTrusted:true,timeStamp:at,
 clientX:at+1,clientY:at+2,button:-1});}
now=600;runTimers();
(async()=>{for(let i=0;i<20;i++)await Promise.resolve();
 process.stdout.write(JSON.stringify(sent.map(item=>item.event)));})();
'''
    result = subprocess.run([node, '-e', script, str(page)], capture_output=True,
                            text=True, timeout=10, check=True)
    events = [event for event in json.loads(result.stdout) if event['type'] == 'pointermove']
    assert [event['timeStamp'] for event in events] == [0, 200, 400]
    assert events == [
        {"type": "pointermove", "isTrusted": True, "target": "drag-source",
         "timeStamp": at, "clientX": at + 1, "clientY": at + 2, "button": -1}
        for at in (0, 200, 400)
    ]


def test_page_geometry_is_passive_false_trust_and_throttled_with_trailing_resize():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is required for isolated page logic checks')
    page = Path(__file__).resolve().parents[1] / 'phonelab/static/lab.html'
    script = r'''
const fs=require('fs'),vm=require('vm'); const handlers={},viewportHandlers={},nodes=new Map(),sent=[];
let now=0,nextTimer=1; const timers=new Map();
class Element { constructor(id=''){this.id=id;this.dataset={};this.style={};this.tagName='DIV';this.textContent='';
 this.value='';this.width=40;this.height=40;}
 getContext(){return {imageSmoothingEnabled:true,createImageData(w,h){return {data:new Uint8ClampedArray(w*h*4)};},putImageData(){}};}
 closest(){return null;} addEventListener(){}
}
const probes=Array.from({length:25},(_,i)=>{const e=new Element();e.dataset={probe:String(i+1).padStart(2,'0')};e.tagName='BUTTON';return e;});
const document={getElementById(id){if(!nodes.has(id))nodes.set(id,new Element(id));return nodes.get(id);},
 querySelectorAll(){return probes;},addEventListener(name,fn){handlers[name]=fn;}};
const viewport={width:390,height:844,offsetLeft:0,offsetTop:0,
 addEventListener(name,fn){viewportHandlers[name]=fn;}};
const window={innerWidth:390,innerHeight:844,devicePixelRatio:3,visualViewport:viewport,
 addEventListener(name,fn){handlers['window:'+name]=fn;}};
function runTimers(){for(const [id,timer] of [...timers])if(timer.at<=now){timers.delete(id);timer.fn();}}
const context={Element,document,window,setInterval(){},performance:{now(){return now;}},
 setTimeout(fn,delay){const id=nextTimer++;timers.set(id,{at:now+delay,fn});return id;},
 clearTimeout(id){timers.delete(id);},fetch(_url,request){sent.push(JSON.parse(request.body));return Promise.resolve({ok:true});}};
const html=fs.readFileSync(process.argv[1],'utf8');
const code=html.match(/<script>([\s\S]*?)<\/script>/)[1].replace('__PHONE_LAB_CONFIG__',
 JSON.stringify({testId:'geometry-only',seed:'1',nonce:'synthetic',runToken:'synthetic'}));
document.getElementById('probe-summary').textContent='Browser-reported probe clicks: 0 | Last: none';
vm.runInNewContext(code,context);
now=25;window.innerWidth=400;viewport.width=380;viewport.height=800;viewport.offsetLeft=10;viewport.offsetTop=20;
handlers['window:resize'](); now=100;viewportHandlers.resize(); now=199;handlers['window:resize']();
now=200;runTimers();
(async()=>{for(let i=0;i<30;i++)await Promise.resolve();
 process.stdout.write(JSON.stringify({events:sent.map(item=>item.event),probe:document.getElementById('probe-summary').textContent}));})();
'''
    result = subprocess.run([node, '-e', script, str(page)], capture_output=True,
                            text=True, timeout=10, check=True)
    observed = json.loads(result.stdout)
    events = [event for event in observed['events'] if event['type'] == 'page_geometry']
    assert len(events) == 2
    assert events[0] == {
        'type': 'page_geometry', 'isTrusted': False, 'target': 'window', 'timeStamp': 0,
        'clientX': None, 'clientY': None, 'button': None,
        'innerWidth': 390, 'innerHeight': 844, 'visualWidth': 390, 'visualHeight': 844,
        'visualOffsetLeft': 0, 'visualOffsetTop': 0, 'devicePixelRatio': 3,
    }
    assert events[1]['timeStamp'] == 200
    assert events[1]['innerWidth'] == 400
    assert events[1]['visualWidth'] == 380 and events[1]['visualHeight'] == 800
    assert events[1]['visualOffsetLeft'] == 10 and events[1]['visualOffsetTop'] == 20
    assert observed['probe'] == 'Browser-reported probe clicks: 0 | Last: none'


def test_scroll_throttle_emits_trailing_real_event_with_latest_scroll_position():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is required for isolated page logic checks')
    page = Path(__file__).resolve().parents[1] / 'phonelab/static/lab.html'
    script = r'''
const fs=require('fs'),vm=require('vm'); const handlers={},nodes=new Map(),sent=[];
let now=0,nextTimer=1; const timers=new Map();
class Element { constructor(id=''){this.id=id;this.dataset={};this.style={};this.tagName='DIV';this.textContent='';
 this.value='';this.width=40;this.height=40;} closest(){return null;} addEventListener(){}
 getContext(){return {imageSmoothingEnabled:true,createImageData(w,h){return {data:new Uint8ClampedArray(w*h*4)};},putImageData(){}};}}
const probes=Array.from({length:25},(_,i)=>{const e=new Element();e.dataset={probe:String(i+1).padStart(2,'0')};return e;});
const document={getElementById(id){if(!nodes.has(id))nodes.set(id,new Element(id));return nodes.get(id);},
 querySelectorAll(){return probes;},addEventListener(name,fn){handlers[name]=fn;}};
const window={scrollX:0,scrollY:0,addEventListener(name,fn){handlers['window:'+name]=fn;}};
function runTimers(){for(const [id,timer] of [...timers])if(timer.at<=now){timers.delete(id);timer.fn();}}
const context={Element,document,window,setInterval(){},performance:{now(){return now;}},
 setTimeout(fn,delay){const id=nextTimer++;timers.set(id,{at:now+delay,fn});return id;},
 clearTimeout(id){timers.delete(id);},fetch(_url,request){sent.push(JSON.parse(request.body));return Promise.resolve({ok:true});}};
const html=fs.readFileSync(process.argv[1],'utf8');
const code=html.match(/<script>([\s\S]*?)<\/script>/)[1].replace('__PHONE_LAB_CONFIG__',
 JSON.stringify({testId:'scroll-only',seed:'1',nonce:'synthetic',runToken:'synthetic'}));
vm.runInNewContext(code,context);
function scroll(at,y){now=at;window.scrollY=y;handlers['window:scroll']({type:'scroll',target:document,isTrusted:true,timeStamp:at});}
scroll(100,0);scroll(110,1);scroll(125,2);now=180;runTimers();
(async()=>{for(let i=0;i<30;i++)await Promise.resolve();
 const events=sent.map(item=>item.event).filter(event=>event.type==='scroll');
 process.stdout.write(JSON.stringify({events,scrollY:window.scrollY}));})();
'''
    result = subprocess.run([node, '-e', script, str(page)], capture_output=True,
                            text=True, timeout=10, check=True)
    observed = json.loads(result.stdout)
    assert [event['timeStamp'] for event in observed['events']] == [100, 125]
    assert [event['scrollY'] for event in observed['events']] == [0, 2]
    assert all(event['isTrusted'] is True for event in observed['events'])


def test_pointermove_throttle_emits_latest_real_event_on_trailing_edge():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is required for isolated page logic checks')
    page = Path(__file__).resolve().parents[1] / 'phonelab/static/lab.html'
    script = r'''
const fs=require('fs'),vm=require('vm'); const handlers={},nodes=new Map(),sent=[];
let now=0,nextTimer=1; const timers=new Map();
class Element { constructor(){this.id='';this.dataset={};this.style={};this.tagName='DIV';this.value='';}
 closest(){return null;} addEventListener(){}
 getContext(){return {imageSmoothingEnabled:true,createImageData(w,h){return {data:new Uint8ClampedArray(w*h*4)};},putImageData(){}};} }
const probes=Array.from({length:25},(_,i)=>{const e=new Element();e.dataset={probe:String(i+1).padStart(2,'0')};e.tagName='BUTTON';return e;});
const document={getElementById(id){if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);},
 querySelectorAll(){return probes;},addEventListener(name,fn){handlers[name]=fn;}};
const window={addEventListener(){}};
function runTimers(){for(const [id,timer] of [...timers])if(timer.at<=now){timers.delete(id);timer.fn();}}
const context={Element,document,window,setInterval(){},performance:{now(){return now;}},
 setTimeout(fn,delay){const id=nextTimer++;timers.set(id,{at:now+delay,fn});return id;},
 clearTimeout(id){timers.delete(id);},fetch(_url,request){sent.push(JSON.parse(request.body));return Promise.resolve({ok:true});}};
const html=fs.readFileSync(process.argv[1],'utf8');
const code=html.match(/<script>([\s\S]*?)<\/script>/)[1].replace('__PHONE_LAB_CONFIG__',
 JSON.stringify({testId:'pointer-only',seed:'1',nonce:'synthetic',runToken:'synthetic'}));
vm.runInNewContext(code,context);
function move(at,timeStamp,x,y,isTrusted=true){now=at;handlers.pointermove({type:'pointermove',target:document,isTrusted,timeStamp,clientX:x,clientY:y,button:-1});}
move(0,10,1,2);move(40,50,3,4);move(130,140,5,6);now=200;runTimers();
move(250,260,7,8);now=400;runTimers();
move(601,611,9,10,false);
(async()=>{for(let i=0;i<30;i++)await Promise.resolve();
 const events=sent.map(item=>item.event).filter(event=>event.type==='pointermove');
 process.stdout.write(JSON.stringify(events));})();
'''
    result = subprocess.run([node, '-e', script, str(page)], capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    events = json.loads(result.stdout)
    assert [(event['timeStamp'], event['clientX'], event['clientY']) for event in events] == [
        (10, 1, 2), (140, 5, 6), (260, 7, 8), (611, 9, 10)]
    assert [event['isTrusted'] for event in events] == [True, True, True, False]
    assert all(event['target'] == 'window' for event in events)


def render_marker_rgb(nonce: str, counter: int) -> list[list[list[int]]]:
    """Return the synthetic 10x10 marker RGB cells, for independent decoder tests."""
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is required for isolated page logic checks')
    page = Path(__file__).resolve().parents[1] / 'phonelab/static/lab.html'
    script = r'''
const fs=require('fs'),vm=require('vm'); const nodes=new Map(); let tick=()=>{};
class Element { constructor(id='',probe=''){this.id=id;this.dataset=probe?{probe}:{};this.style={};
 this.tagName='BUTTON';this.textContent='';this.value='';this.width=40;this.height=40;}
 closest(sel){return sel==='.probe'&&this.dataset.probe?this:null;} addEventListener(){}
 getContext(){const canvas=this;return {imageSmoothingEnabled:true,
  createImageData(w,h){return {data:new Uint8ClampedArray(w*h*4)};},
  putImageData(image){canvas.image=image;}};}}
const probes=Array.from({length:25},(_,i)=>new Element('',String(i+1).padStart(2,'0')));
const document={getElementById(id){if(!nodes.has(id))nodes.set(id,new Element(id));return nodes.get(id);},
 querySelectorAll(){return probes;},addEventListener(){}};
const canvas=document.getElementById('visual-marker');
const context={Element,document,window:{scrollX:0,scrollY:0,addEventListener(){}},
 setInterval(fn){tick=fn;},fetch(){return Promise.resolve({ok:true});},performance:{now(){return 0;}}};
const html=fs.readFileSync(process.argv[1],'utf8');
const code=html.match(/<script>([\s\S]*?)<\/script>/)[1].replace('__PHONE_LAB_CONFIG__',
 JSON.stringify({testId:'synthetic-test',seed:'1',nonce:process.argv[2],runToken:'never-rendered'}));
vm.runInNewContext(code,context); for(let i=0;i<Number(process.argv[3]);i++)tick();
const rgb=[];for(let r=0;r<10;r++){const row=[];for(let c=0;c<10;c++){
 const p=((r*4+2)*40+c*4+2)*4;row.push(Array.from(canvas.image.data.slice(p,p+3)));}rgb.push(row);}
process.stdout.write(JSON.stringify(rgb));
'''
    result = subprocess.run([node, '-e', script, str(page), nonce, str(counter)],
                            capture_output=True, text=True, timeout=10, check=True)
    return json.loads(result.stdout)


def _fnv1a32_ascii(value: str) -> int:
    result = 0x811C9DC5
    for byte in value.encode('ascii'):
        result = ((result ^ byte) * 0x01000193) & 0xFFFFFFFF
    return result


def _crc16_ccitt_false(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ (0x1021 if crc & 0x8000 else 0)) & 0xFFFF
    return crc


def test_visual_marker_raster_encodes_nonce_counter_and_crc_with_fixed_border():
    nonce, counter = 'synthetic-nonce-7f31', 0x1234
    rgb = render_marker_rgb(nonce, counter)
    assert len(rgb) == 10 and all(len(row) == 10 for row in rgb)
    cyan, magenta, yellow, blue = [0, 200, 220], [220, 0, 200], [250, 200, 0], [30, 80, 240]
    assert rgb[0] == [cyan] * 10
    assert rgb[9] == [magenta] * 10
    assert [rgb[row][0] for row in range(1, 9)] == [yellow] * 8
    assert [rgb[row][9] for row in range(1, 9)] == [blue] * 8
    prefix = _fnv1a32_ascii(nonce).to_bytes(4, 'big') + counter.to_bytes(2, 'big')
    payload = prefix + _crc16_ccitt_false(prefix).to_bytes(2, 'big')
    expected_bits = [((byte >> shift) & 1) for byte in payload for shift in range(7, -1, -1)]
    black, white = [20, 20, 20], [250, 250, 250]
    actual_bits = [rgb[row][col] for row in range(1, 9) for col in range(1, 9)]
    assert actual_bits == [black if bit else white for bit in expected_bits]
    assert render_marker_rgb(nonce, counter + 1) != rgb
