from argparse import ArgumentParser
import asyncio
import json
from pathlib import Path
import threading
import time
from .paths import resolve_data_root

DEFAULT_DATA = resolve_data_root()


def main():
    p = ArgumentParser(description='Local PhoneLab; default OEM preservation, no firmware write commands')
    p.add_argument('--data-root', type=Path, default=DEFAULT_DATA)
    commands = p.add_subparsers(dest='command', required=True)
    status = commands.add_parser('status')
    status.add_argument('--refresh', action='store_true')
    status.add_argument('--probe-screen', action='store_true')
    serve = commands.add_parser('serve')
    serve.add_argument('--port', type=int, default=8766)
    serve.add_argument('--wait-for-device', action='store_true')
    serve.add_argument('--capture-usb', action='store_true',
                       help='explicitly start one read-only USB screenshot session')
    backup = commands.add_parser('read-backup')
    backup.add_argument('--port', required=True)
    backup.add_argument('--confirmed-instance', required=True)
    backup.add_argument('--output', type=Path, required=True)
    preview = commands.add_parser('restore-preview')
    preview.add_argument('manifest', type=Path)
    commands.add_parser('stop')
    args = p.parse_args()
    root = args.data_root
    root.mkdir(parents=True, exist_ok=True)
    if args.command == 'status':
        from .iphone import inspect_sync
        if args.refresh:
            result = inspect_sync(root, args.probe_screen)
            (root / 'status.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        else:
            state_path = root / 'status.json'
            result = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'state':'UNKNOWN'}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == 'read-backup':
        from .backup import read_confirmed_board
        # Output contains only sanitized state; esptool output stays in caller-selected private log.
        result = read_confirmed_board(args.port, args.output, args.confirmed_instance)
        print(json.dumps({'state': result['state'], 'size': result['range']['size'], 'restore_tested': False}))
    elif args.command == 'restore-preview':
        from .backup import restore_preview
        print(json.dumps(restore_preview(args.manifest), indent=2))
    elif args.command == 'stop':
        import urllib.request
        from .stop_signal import StopStore
        # Publish first; external owners must stop even when HTTP is offline.
        published = False
        try:
            StopStore(root / 'private').publish_stop()
            published = True
        except Exception:
            pass
        result = {'shared_stop': 'published' if published else 'failed', 'local_controller_stopped': False,
                  'release_confirmed': False}
        try:
            cfg = json.loads((root / 'private/runtime.json').read_text(encoding='utf-8'))
            if type(cfg.get('port')) is not int or not 1 <= cfg['port'] <= 65535:
                raise ValueError('invalid local port')
            url = f"http://127.0.0.1:{cfg['port']}"
            request = urllib.request.Request(url + '/stop', data=b'{}', method='POST',
                  headers={'Content-Type':'application/json', 'Origin':url, 'X-PhoneLab-Control':cfg['token']})
            with urllib.request.urlopen(request, timeout=3) as response:
                body = json.loads(response.read())
                result['local_controller_stopped'] = body.get('local_controller_stopped') is True
        except Exception:
            pass
        print(json.dumps(result))
        return 0 if published else 1
    elif args.command == 'serve':
        from .control import Controller
        from .iphone import inspect_sync, wait_for_bound_usb
        from .web import create_server
        previous = root / 'status.json'
        observed = json.loads(previous.read_text(encoding='utf-8')) if previous.exists() else {}
        controller = Controller(observed.get('target', 'unbound'))
        screen_runtime = None
        if args.capture_usb:
            from .screen_runtime import ScreenCaptureRuntime
            if args.wait_for_device:
                print('Waiting for the confirmed USB target; enumeration only; Ctrl+C cancels', flush=True)
                asyncio.run(wait_for_bound_usb(root))
            screen_runtime = ScreenCaptureRuntime(root)
            screen_runtime.start()
        try:
            server = create_server('127.0.0.1', args.port, root, controller,
                                   screen_runtime=screen_runtime)
        except Exception:
            if screen_runtime is not None:
                screen_runtime.close()
            raise
        runtime = root / 'private/runtime.json'
        quit_event = threading.Event()
        def monitor():
            next_probe = 0
            capture_was_fresh = False
            while not quit_event.wait(.1):
                controller.watchdog()
                if screen_runtime is not None:
                    try:
                        capture_fresh = screen_runtime.status().get('fresh') is True
                    except Exception:
                        capture_fresh = False
                    if capture_was_fresh and not capture_fresh:
                        try:
                            server.stop_store.publish_stop()
                        except Exception:
                            print('PhoneLab shared stop publication failed', flush=True)
                        controller.stop()
                    capture_was_fresh = capture_fresh
                if args.wait_for_device and not args.capture_usb and time.monotonic() >= next_probe:
                    state = inspect_sync(root, False)
                    (root / 'status.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
                    next_probe = time.monotonic() + 10
                    # No backend creation, firmware writing, pairing, or queue replay on arrival.
        thread = threading.Thread(target=monitor, daemon=True)
        try:
            (root / 'private').mkdir(parents=True, exist_ok=True)
            runtime.write_text(json.dumps({'port':server.server_port, 'token':server.control_token}), encoding='utf-8')
            thread.start()
            capture_state = 'USB screenshot capture active' if screen_runtime else 'USB screenshot capture NONE'
            print(f'PhoneLab local service http://127.0.0.1:{server.server_port}/lab ; input backend NONE; {capture_state}; Ctrl+C to exit', flush=True)
            server.serve_forever(poll_interval=.1)
        except KeyboardInterrupt:
            pass
        finally:
            quit_event.set()
            try:
                server.stop_store.publish_stop()
            except Exception:
                print('PhoneLab shared stop publication failed during shutdown', flush=True)
            controller.stop()
            server.server_close()
            if thread.is_alive():
                thread.join(timeout=2)
            runtime.unlink(missing_ok=True)
            if screen_runtime is not None:
                try:
                    screen_runtime.close()
                finally:
                    # Only enumerated failure codes/booleans; no traceback/context.
                    diagnostic = screen_runtime.shutdown_status()
                    diagnostic['recorded_monotonic'] = time.monotonic()
                    diagnostic_path = root / 'private' / ('capture-shutdown-' + str(time.monotonic_ns()) + '.json')
                    diagnostic_path.write_text(json.dumps(diagnostic, indent=2), encoding='utf-8')


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print(json.dumps({'state': 'CANCELLED'}))
        raise SystemExit(130)
    except Exception as exc:
        # Device/library exception strings may contain identifiers. Do not echo them.
        print(json.dumps({'state':'FAILED', 'error_type':type(exc).__name__}))
        raise SystemExit(1)
