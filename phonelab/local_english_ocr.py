"""Offline, project-owned English OCR; no network endpoint or raw text output."""
import atexit
import json
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading

from .deploy import DATA
from .page_scope import _synthetic_text_prefix
from .synthetic_vision import _srgb_image
from .visual_marker import decode_visual_marker


class _Worker:
    def __init__(self):
        node=shutil.which('node')
        if not node:raise RuntimeError('local English OCR requires Node')
        script=Path(__file__).resolve().parents[1]/'scripts/local-ocr/worker.cjs'
        self.process=subprocess.Popen([node,str(script),str(DATA/'tooling/local-ocr/tessdata')],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            text=True,encoding='utf-8',bufsize=1)
        self.rows=queue.Queue();self.lock=threading.Lock();self.counter=0
        def read():
            for line in self.process.stdout:self.rows.put(line)
            self.rows.put(None)
        threading.Thread(target=read,daemon=True,name='phonelab-local-ocr').start()
        try:
            if self._read(20).get('ready') is not True:raise RuntimeError()
        except Exception:
            self.close();raise RuntimeError('local English OCR unavailable') from None

    def _read(self,timeout):
        line=self.rows.get(timeout=timeout)
        if line is None:raise RuntimeError('local OCR closed')
        return json.loads(line)

    def recognize(self,path,expected):
        with self.lock:
            self.counter+=1
            job={'id':self.counter,'imagePath':str(path),'expectedText':expected}
            self.process.stdin.write(json.dumps(job)+'\n');self.process.stdin.flush()
            reply=self._read(5)
            if reply.get('id')!=self.counter or 'error_code' in reply:raise RuntimeError('local OCR failed')
            return reply

    def close(self):
        try:self.process.stdin.close();self.process.wait(timeout=2)
        except Exception:
            try:self.process.kill();self.process.wait(timeout=2)
            except Exception:pass


_worker=None


def prepare_text_ocr():
    """Warm up before selecting any frame that may authorize input."""
    global _worker
    if _worker is None:_worker=_Worker()
    return _worker


def close_text_ocr():
    global _worker
    if _worker is not None:_worker.close();_worker=None


atexit.register(close_text_ocr)


def read_text_field_ocr(png,nonce,bounds,expected_text,caret_bounds,*,private_root=None,reader=None):
    from .page_experiments import _private_root
    if not _synthetic_text_prefix(expected_text):raise ValueError('unsupported synthetic text')
    decode_visual_marker(png,nonce)
    for box in (bounds,caret_bounds):
        if (not isinstance(box,(tuple,list)) or len(box)!=4 or any(type(v) is not int for v in box)
                or not 0<=box[0]<box[2]<=828 or not 0<=box[1]<box[3]<=1792):raise ValueError('invalid field bounds')
    l,t,r,b=caret_bounds
    if (not bounds[0]<l<r<bounds[2] or not bounds[1]<t<b<bounds[3]
            or not 1<=r-l<=5 or not 25<=b-t<=60):raise ValueError('invalid native caret bounds')
    root=_private_root(Path(private_root) if private_root else DATA/'private')
    root.mkdir(parents=True,exist_ok=True)
    try:
        image=_srgb_image(png);pixels=image.load()
        for y in range(t,b):
            for x in range(l,r):
                red,green,blue=pixels[x,y]
                if 200<=red<=245 and 10<=green<=55 and 45<=blue<=105:pixels[x,y]=(232,255,244)
        # Only the field interior reaches the local recognizer. Original evidence
        # and its independently validated native caret stay unchanged.
        image=image.crop((bounds[0]+8,bounds[1]+6,bounds[2]-8,bounds[3]-6))
        if expected_text=='':
            # Blank recognizers can hallucinate glyphs. Absence of native text
            # ink, plus the separate visible caret-at-start/focus checks, is
            # the stronger empty-field condition.
            from PIL import ImageChops
            channels=image.split()
            masks=[channel.point([255 if value<limit else 0 for value in range(256)])
                   for channel,limit in zip(channels,(80,100,120))]
            empty=ImageChops.multiply(ImageChops.multiply(masks[0],masks[1]),masks[2]).getbbox() is None
            return {'text_matches':empty,'recognized_length':0 if empty else None}
        recognize=reader or prepare_text_ocr().recognize
        with tempfile.TemporaryDirectory(prefix='field-ocr-',dir=root) as temporary:
            path=Path(temporary)/'field.png';image.save(path,format='PNG')
            result=recognize(path,expected_text)
        matches=result.get('text_matches');length=result.get('recognized_length')
        if type(matches) is not bool or (length is not None and type(length) is not int):raise ValueError()
        return {'text_matches':matches,'recognized_length':length}
    except Exception:
        close_text_ocr()
        raise RuntimeError('local synthetic field OCR unavailable') from None
