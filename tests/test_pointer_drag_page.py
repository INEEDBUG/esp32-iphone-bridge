"""Isolated JS behavior; never real-phone evidence."""
import json
from pathlib import Path
import shutil
import subprocess
import pytest


def test_pointer_drag_requires_trusted_press_move_release_and_reports_derived_result():
    node=shutil.which('node')
    if not node:pytest.skip('Node unavailable')
    code=r'''
const fs=require('fs'),vm=require('vm'),nodes=new Map(),sent=[],handlers={};let prevented=0;
class Element {constructor(id=''){this.id=id;this.dataset={};this.style={};this.handlers={};this.tagName='DIV';this.textContent='';this.value='';}
getContext(){return {createImageData(w,h){return {data:new Uint8ClampedArray(w*h*4)};},putImageData(){}};}
closest(){return null;} addEventListener(n,f){this.handlers[n]=f;}setPointerCapture(){}
getBoundingClientRect(){return {left:150,top:100,right:300,bottom:200};}}
const probes=Array.from({length:25},(_,i)=>{const e=new Element();e.dataset.probe=String(i+1).padStart(2,'0');return e;});
const document={getElementById(id){if(!nodes.has(id))nodes.set(id,new Element(id));return nodes.get(id);},querySelectorAll(){return probes;},addEventListener(n,f){handlers[n]=f;}};
const context={Element,document,window:{scrollX:0,scrollY:0,addEventListener(){}},performance:{now(){return 0;}},setInterval(){},fetch(u,r){sent.push(JSON.parse(r.body).event);return Promise.resolve({ok:true});}};
const html=fs.readFileSync(process.argv[1],'utf8');
vm.runInNewContext(html.match(/<script>([\s\S]*?)<\/script>/)[1].replace('__PHONE_LAB_CONFIG__',JSON.stringify({testId:'unit',seed:'42',nonce:'test',runToken:'test'})),context);
const source=document.getElementById('drag-source');
function drag(trusted,move){for(const kind of ['pointerdown',...(move?['pointermove']:[]),'pointerup'])source.handlers[kind]({isTrusted:trusted,button:0,buttons:1,pointerId:1,clientX:200,clientY:150,preventDefault(){prevented++;}});}
drag(false,true); const untrusted=document.getElementById('drop-state').textContent;
drag(true,false); drag(true,true);
(async()=>{for(let i=0;i<30;i++)await Promise.resolve();process.stdout.write(JSON.stringify({untrusted,prevented,final:document.getElementById('drop-state').textContent,results:sent.filter(e=>e.type==='drag_result')}));})();
'''
    page=Path(__file__).resolve().parents[1]/'phonelab/static/lab.html'
    run=subprocess.run([node,'-e',code,str(page)],capture_output=True,text=True,timeout=10,check=True)
    observed=json.loads(run.stdout)
    assert observed['untrusted']==''
    assert observed['prevented']==2  # only the two trusted primary presses cancel selection
    assert observed['final']=='Completed drags: 1'
    assert [item['completed'] for item in observed['results']]==[False,True]
    assert all(item['isTrusted'] is False for item in observed['results'])
