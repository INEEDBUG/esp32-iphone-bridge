import json
from pathlib import Path
import shutil
import subprocess
import pytest


def test_worker_exact_match_does_not_trim_spaces_or_return_recognized_text():
    node=shutil.which('node')
    if not node:pytest.skip('Node unavailable')
    worker=Path(__file__).resolve().parents[1]/'scripts/local-ocr/worker.cjs'
    script=r'''
const worker=require(process.argv[1]);
const results=['test123\n','test123 ','TEST123','private message'].map(raw=>worker.matchText(raw,'test123'));
let refused=false;try{worker.validateJob({id:1,imagePath:'https://example.com/image.png',expectedText:'private message'});}catch(e){refused=true;}
process.stdout.write(JSON.stringify({results,refused}));
'''
    run=subprocess.run([node,'-e',script,str(worker)],capture_output=True,text=True,timeout=10,check=True)
    observed=json.loads(run.stdout)
    assert observed['refused']
    assert observed['results'][0]=={'text_matches':True,'recognized_length':7}
    assert all(item=={'text_matches':False,'recognized_length':None} for item in observed['results'][1:])
