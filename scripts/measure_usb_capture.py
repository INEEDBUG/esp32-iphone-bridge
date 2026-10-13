"""Measure the existing screenshot service; never sends input or views private pixels."""
import argparse
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path
import time
import urllib.request

from PIL import Image
from phonelab.paths import resolve_data_root

DATA = resolve_data_root()


def validate_status(status, target, now, previous_sequence=0):
    if (not isinstance(status, dict) or status.get('armed') is not False or
            status.get('ACTIVE_INPUT_BACKEND') != 'none'):
        raise ValueError('capture_only_input_boundary_changed')
    frame = status.get('screen_capture', {})
    sequence = frame.get('sequence')
    received = frame.get('received_monotonic')
    if (frame.get('target') != target or frame.get('fresh') is not True or
            frame.get('source') != 'USB_SCREENSHOT' or frame.get('source_time') is not None or
            frame.get('locked') != 'UNKNOWN' or type(sequence) is not int or sequence < 1 or
            sequence < previous_sequence or type(received) not in (int, float) or
            not math.isfinite(received) or not 0 <= now - received <= 2):
        raise ValueError('capture_target_sequence_or_freshness_invalid')
    if (frame.get('width'), frame.get('height'), frame.get('orientation')) != (828, 1792, 'portrait'):
        raise ValueError('capture_geometry_changed')
    return {'sequence': sequence, 'age_seconds': now - received}


def measure(seconds=900):
    if type(seconds) is not int or not 1 <= seconds <= 3600:
        raise ValueError('duration_outside_bounds')
    private = DATA / 'private'
    config = json.loads((private / 'runtime.json').read_text(encoding='utf-8'))
    binding = json.loads((private / 'target.json').read_text(encoding='utf-8'))
    if config.get('port') != 8766:
        raise ValueError('unexpected_project_port')
    base = 'http://127.0.0.1:8766'
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = private / ('usb-capture-stability-' + stamp + '.json')
    result = {'scope': 'USB_CAPTURE_ONLY; NOT phone-input/BLE stability', 'state': 'RUNNING',
              'requested_seconds': seconds, 'samples': [], 'png_checks': 0,
              'input_sent': False, 'images_sent_to_model': False}
    started = time.monotonic()
    previous, next_png, next_progress = 0, started, started + 60

    def save():
        result['elapsed_seconds'] = round(time.monotonic() - started, 3)
        output.write_text(json.dumps(result, indent=2), encoding='utf-8')

    try:
        while time.monotonic() - started < seconds:
            with urllib.request.urlopen(base + '/status', timeout=4) as response:
                status = json.load(response)
            sample = validate_status(status, binding['target'], time.monotonic(), previous)
            sample['elapsed_seconds'] = round(time.monotonic() - started, 3)
            result['samples'].append(sample)
            previous = sample['sequence']
            if time.monotonic() >= next_png:
                request = urllib.request.Request(base + '/screen', headers={
                    'Origin': base, 'X-PhoneLab-Control': config['token']})
                with urllib.request.urlopen(request, timeout=4) as response:
                    raw = response.read(20 * 1024 * 1024 + 1)
                if len(raw) > 20 * 1024 * 1024:
                    raise ValueError('oversized_screen_response')
                with Image.open(io.BytesIO(raw)) as picture:
                    if picture.format != 'PNG' or picture.size != (828, 1792):
                        raise ValueError('unexpected_screen_payload')
                    picture.verify()
                result['png_checks'] += 1
                next_png = time.monotonic() + 30
            if time.monotonic() >= next_progress:
                save()
                print(f"CAPTURE_ONLY_PROGRESS seconds={int(result['elapsed_seconds'])} samples={len(result['samples'])}", flush=True)
                next_progress = time.monotonic() + 60
            time.sleep(min(1, max(0, seconds - (time.monotonic() - started))))
        result['state'] = 'COMPLETED_CAPTURE_ONLY'
    except Exception as exc:
        result.update(state='FAILED', error_type=type(exc).__name__)
    finally:
        save()
    print(json.dumps({'state': result['state'], 'seconds': result['elapsed_seconds'],
                      'samples': len(result['samples']), 'png_checks': result['png_checks'],
                      'input_sent': False, 'evidence_file': output.name}), flush=True)
    return 0 if result['state'] == 'COMPLETED_CAPTURE_ONLY' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=900)
    arguments = parser.parse_args()
    try:
        raise SystemExit(measure(arguments.seconds))
    except Exception as exc:
        print(json.dumps({'state': 'FAILED', 'error_type': type(exc).__name__}))
        raise SystemExit(1)
