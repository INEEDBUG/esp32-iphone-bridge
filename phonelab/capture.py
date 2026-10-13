"""Read received JPEGs without pretending local file time is phone capture time."""
from pathlib import Path
import time
from PIL import Image


class CaptureError(RuntimeError):
    pass


class CaptureDirectory:
    def __init__(self, directory, target, binding_verified=False, generation=None, max_age=2):
        self.directory = Path(directory)
        self.target = target
        self.binding_verified = binding_verified
        self.generation = generation
        self.max_age = max_age
        self._fingerprint = None
        self._metadata = None
        self._sequence = 0

    def latest(self):
        if not self.binding_verified or not self.target:
            raise CaptureError('AirPlay target binding is unverified')
        if 'offline' in str(self.directory).lower() or 'synthetic' in str(self.directory).lower():
            raise CaptureError('offline synthetic images cannot be phone evidence')
        candidates = list(self.directory.glob('frame-*.jpg'))
        if not candidates: raise CaptureError('no phone frames received')
        path = max(candidates, key=lambda p:p.stat().st_mtime_ns)
        before = path.stat()
        age = time.time() - before.st_mtime
        if not 0 <= age <= self.max_age: raise CaptureError('stale or future-dated received file')
        fingerprint = (str(path), before.st_mtime_ns, before.st_size)
        if fingerprint == self._fingerprint:
            if time.monotonic()-self._metadata['received_monotonic'] > self.max_age:
                raise CaptureError('capture is not advancing')
            return dict(self._metadata)
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                width, height = image.size
        except Exception:
            raise CaptureError('incomplete or invalid received JPEG') from None
        after = path.stat()
        if (after.st_mtime_ns,after.st_size) != (before.st_mtime_ns,before.st_size):
            raise CaptureError('capture changed while being read')
        self._sequence += 1
        self._fingerprint = fingerprint
        self._metadata = {
            'target':self.target,'sequence':self._sequence,'generation':self.generation,
            'source':'UxPlay/GStreamer JPEG receive pipeline',
            'source_time':None,'source_time_available':False,
            'received_monotonic':time.monotonic(), 'file_completed_wall_time':before.st_mtime,
            'width':width,'height':height,
            'orientation':'portrait' if height > width else 'UNKNOWN',
            'locked':None, 'private_path':str(path.resolve()),
            'note':'Target binding must be established independently on the actual phone/test page. File time is not phone capture time. Lock state unverified; input stays blocked.'}
        return dict(self._metadata)
