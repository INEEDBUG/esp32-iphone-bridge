import sys

import pytest

from phonelab.__main__ import main
from phonelab import iphone, screen_runtime


def test_stop_cli_cancels_external_tokens_even_without_http_service(tmp_path, monkeypatch, capsys):
    import json
    import urllib.request
    from phonelab.stop_signal import StopStore
    token = StopStore(tmp_path / 'private').capture()
    def offline(*args, **kwargs): raise OSError('private failure not logged')
    monkeypatch.setattr(urllib.request, 'urlopen', offline)
    monkeypatch.setattr(sys, 'argv', ['phonelab', '--data-root', str(tmp_path), 'stop'])
    main()
    result = json.loads(capsys.readouterr().out)
    assert token.is_set()
    assert result['shared_stop'] == 'published'
    assert result['local_controller_stopped'] is False
    assert result['release_confirmed'] is False


def test_stop_cli_attempts_http_cleanup_even_if_shared_publication_fails(tmp_path, monkeypatch, capsys):
    import io
    import json
    import urllib.request
    from phonelab.stop_signal import StopStore
    private = tmp_path / 'private'
    private.mkdir()
    (private / 'runtime.json').write_text(json.dumps({'port': 8766, 'token': 'test-control-token'}))
    def failed(_store): raise OSError('private store error')
    monkeypatch.setattr(StopStore, 'publish_stop', failed)
    called = []
    def local_stop(*args, **kwargs):
        called.append(True)
        return io.BytesIO(b'{"local_controller_stopped":true}')
    monkeypatch.setattr(urllib.request, 'urlopen', local_stop)
    monkeypatch.setattr(sys, 'argv', ['phonelab', '--data-root', str(tmp_path), 'stop'])
    assert main() == 1
    result = json.loads(capsys.readouterr().out)
    assert called == [True] and result['local_controller_stopped'] is True
    assert result['shared_stop'] == 'failed' and result['release_confirmed'] is False


@pytest.mark.parametrize('wait', [False, True])
def test_capture_startup_waits_only_when_requested_and_never_selects_input(tmp_path, monkeypatch, wait):
    order = []
    class ProbeFinished(Exception): pass
    class Capture:
        def __init__(self, root): assert root == tmp_path
        def start(self):
            order.append('capture')
            raise ProbeFinished()
    async def arrival(root):
        assert root == tmp_path
        order.append('arrival')
    monkeypatch.setattr(iphone, 'wait_for_bound_usb', arrival)
    monkeypatch.setattr(iphone, 'inspect_sync', lambda *_: pytest.fail('arrival must not probe service capabilities'))
    monkeypatch.setattr(screen_runtime, 'ScreenCaptureRuntime', Capture)
    argv = ['phonelab', '--data-root', str(tmp_path), 'serve', '--capture-usb']
    if wait: argv.append('--wait-for-device')
    monkeypatch.setattr(sys, 'argv', argv)
    with pytest.raises(ProbeFinished):
        main()
    assert order == (['arrival', 'capture'] if wait else ['capture'])
