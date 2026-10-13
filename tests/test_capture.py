import os
import time
import pytest
from PIL import Image
from phonelab.capture import CaptureDirectory, CaptureError


def frame(path):
    Image.new('RGB', (39,84), 'white').save(path)


def test_unverified_binding_cannot_be_reported_as_phone_capture(tmp_path):
    frame(tmp_path/'frame-0001.jpg')
    with pytest.raises(CaptureError): CaptureDirectory(tmp_path, 'target').latest()


def test_repeated_read_does_not_refresh_old_capture(tmp_path):
    path = tmp_path/'frame-0001.jpg'
    frame(path)
    c = CaptureDirectory(tmp_path,'target',binding_verified=True)
    a, b = c.latest(), c.latest()
    assert a['sequence'] == b['sequence']
    assert a['received_monotonic'] == b['received_monotonic']
    assert a['source_time'] is None
    assert a['source_time_available'] is False


def test_old_or_partial_file_is_rejected(tmp_path):
    path = tmp_path/'frame-0001.jpg'
    frame(path)
    os.utime(path,(time.time()-10,time.time()-10))
    c = CaptureDirectory(tmp_path,'target',binding_verified=True)
    with pytest.raises(CaptureError):c.latest()
    path.write_bytes(b'partial')
    with pytest.raises(CaptureError):c.latest()


def test_synthetic_directory_never_becomes_phone_source(tmp_path):
    root = tmp_path/'airplay-offline-validation'
    root.mkdir()
    frame(root/'frame-0001.jpg')
    with pytest.raises(CaptureError): CaptureDirectory(root,'target',binding_verified=True).latest()
