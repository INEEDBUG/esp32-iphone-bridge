"""Explicitly download one pinned official English OCR data file, outside Git."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from phonelab.paths import resolve_data_root

MODEL_COMMIT='87416418657359cb625c412a48b6e1d6d41c29bd'
MODEL_SHA256='7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2'
MODEL_SIZE=4113088
MODEL_URL=f'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{MODEL_COMMIT}/eng.traineddata'


def prepare_data(data_root,*,download=False,fetch=None):
    root=Path(data_root).resolve();project=Path(__file__).resolve().parents[1]
    if root==project or project in root.parents:raise ValueError('OCR data must remain outside source')
    target=root/'tooling/local-ocr/tessdata/eng.traineddata'
    if target.exists():
        payload=target.read_bytes()
    elif not download:return {'state':'PREVIEW_ONLY','sha256':MODEL_SHA256,'size':MODEL_SIZE}
    elif fetch is not None:payload=fetch(MODEL_URL)
    else:
        with urllib.request.urlopen(MODEL_URL,timeout=30) as response:payload=response.read(MODEL_SIZE+1)
    if len(payload)!=MODEL_SIZE or hashlib.sha256(payload).hexdigest()!=MODEL_SHA256:
        raise ValueError('English OCR data checksum mismatch; existing file preserved')
    if not target.exists():
        target.parent.mkdir(parents=True,exist_ok=True)
        with target.open('xb') as stream:stream.write(payload)
    return {'state':'VERIFIED_LOCAL_DATA','sha256':MODEL_SHA256,'size':MODEL_SIZE}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root',type=Path,default=resolve_data_root())
    parser.add_argument('--download',action='store_true')
    args=parser.parse_args()
    print(json.dumps(prepare_data(args.data_root,download=args.download),sort_keys=True))
