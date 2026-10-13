import asyncio
import hashlib
import tempfile
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

import phonelab.usb_capture as capture


def png_bytes(color=(20, 40, 80)):
    buffer = BytesIO()
    Image.new("RGB", (64, 96), color).save(buffer, format="PNG")
    return buffer.getvalue()


def private_tempdir():
    private = capture.TASK_DATA_ROOT / "private"
    private.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix="usb-capture-test-", dir=private)


def target_for(serial):
    return "iphone-" + hashlib.sha256(serial.encode("utf-8")).hexdigest()[:12]


class FakeTunnel:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.rsd = type("RSD", (), {
            "udid": kwargs.get("serial"),
            "product_type": capture.TARGET_MODEL,
            "product_version": capture.TARGET_OS,
            "product_build_version": capture.TARGET_BUILD,
        })()

    async def __aenter__(self):
        return self.rsd

    async def __aexit__(self, *_args):
        self.closed = True


class FakeContext:
    def __init__(self, value):
        self.value = value
        self.closed = False

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        self.closed = True


class FakeScreenshot:
    def __init__(self, _dvt, payloads):
        self.payloads = payloads
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    async def get_screenshot(self):
        return self.payloads.pop(0)


def test_usb_capture_is_explicit_bounded_and_reports_unknown_lock_state():
    with private_tempdir() as tmp:
        private_root = Path(tmp)

        async def scenario():
            opened = []
            payloads = [png_bytes(), png_bytes()]

            def tunnel_factory(**kwargs):
                value = FakeTunnel(**kwargs)
                opened.append(value)
                return value

            provider = capture.USBScreenCapture(
                serial="EXACT-USB-SERIAL",
                target=target_for("EXACT-USB-SERIAL"),
                data_root=capture.TASK_DATA_ROOT,
                output_directory=private_root / "usb-frames",
                tunnel_factory=tunnel_factory,
                dvt_factory=lambda rsd: FakeContext(rsd),
                screenshot_factory=lambda dvt: FakeScreenshot(dvt, payloads),
                dependency_verifier=lambda: True,
                ring_size=2,
                max_frames=3,
            )
            await provider.open()
            assert opened[0].kwargs == {
                "serial": "EXACT-USB-SERIAL",
                "autopair": False,
                "remotepairing_fallback": False,
                "connection_type": "USB",
                "pairing_records_cache_folder": capture.TASK_DATA_ROOT.resolve() / "private" / "pairing-cache",
            }
            first = await provider.capture()
            second = await provider.capture()
            assert first["frame"].sequence == 1 and second["frame"].sequence == 2
            assert first["frame"].locked is None and first["lock_state"] == "UNKNOWN"
            assert first["frame"].source_time is None
            assert first["frame"].width == 64 and first["frame"].height == 96
            assert first["image_bytes"] == png_bytes()
            assert first["private_path"].read_bytes() == png_bytes()
            await provider.close()
            assert opened[0].closed

        asyncio.run(scenario())


def test_usb_capture_ring_removes_only_its_oldest_frames():
    with private_tempdir() as tmp:
        private_root = Path(tmp)

        async def scenario():
            provider = capture.USBScreenCapture(
                serial="TEST-SERIAL",
                target=target_for("TEST-SERIAL"),
                data_root=capture.TASK_DATA_ROOT,
                output_directory=private_root / "usb-frames",
                tunnel_factory=lambda **kwargs: FakeTunnel(**kwargs),
                dvt_factory=lambda rsd: FakeContext(rsd),
                screenshot_factory=lambda dvt: FakeScreenshot(dvt, [png_bytes() for _ in range(3)]),
                dependency_verifier=lambda: True,
                ring_size=2,
            )
            await provider.open()
            frames = [await provider.capture() for _ in range(3)]
            assert frames[0]["private_path"] == frames[2]["private_path"]
            assert frames[1]["private_path"].exists()
            assert frames[2]["private_path"].exists()
            assert len(list((private_root / "usb-frames" / "ring").glob("*.png"))) == 2
            await provider.close()

        asyncio.run(scenario())


def test_usb_capture_refuses_wrong_identity_and_closes_tunnel():
    with private_tempdir() as tmp:
        private_root = Path(tmp)

        async def scenario():
            tunnel = FakeTunnel()
            tunnel.rsd.udid = "OTHER-USB-SERIAL"
            provider = capture.USBScreenCapture(
                serial="TEST-SERIAL",
                target=target_for("TEST-SERIAL"),
                data_root=capture.TASK_DATA_ROOT,
                output_directory=private_root / "usb-frames",
                tunnel_factory=lambda **_kwargs: tunnel,
                dvt_factory=lambda rsd: FakeContext(rsd),
                screenshot_factory=lambda dvt: FakeScreenshot(dvt, []),
                dependency_verifier=lambda: True,
            )
            with pytest.raises(capture.CaptureError):
                await provider.open()
            assert tunnel.closed

        asyncio.run(scenario())


def test_target_label_must_hash_the_exact_usb_serial():
    with private_tempdir() as tmp:
        with pytest.raises(capture.CaptureError):
            capture.USBScreenCapture(
                serial="EXPECTED-SERIAL",
                target="iphone-000000000000",
                data_root=capture.TASK_DATA_ROOT,
                output_directory=Path(tmp) / "usb-frames",
                dependency_verifier=lambda: True,
            )


def test_three_hour_session_bound_uses_clock_injection_and_closes_resources():
    with private_tempdir() as tmp:
        now = [0.0]
        tunnel = FakeTunnel(serial="TEST-SERIAL")
        dvt = FakeContext(tunnel.rsd)
        screenshot = FakeScreenshot(dvt, [png_bytes()])

        async def scenario():
            provider = capture.USBScreenCapture(
                serial="TEST-SERIAL",
                target=target_for("TEST-SERIAL"),
                data_root=capture.TASK_DATA_ROOT,
                output_directory=Path(tmp) / "usb-frames",
                tunnel_factory=lambda **_kwargs: tunnel,
                dvt_factory=lambda _rsd: dvt,
                screenshot_factory=lambda _dvt: screenshot,
                dependency_verifier=lambda: True,
                clock=lambda: now[0],
                session_timeout=1,
            )
            await provider.open()
            now[0] = 1
            with pytest.raises(capture.CaptureError, match="time limit"):
                await provider.capture()
            assert provider.cleanup_status == "CLOSED"
            assert tunnel.closed and dvt.closed and screenshot.closed

        asyncio.run(scenario())


def test_cancel_during_provider_owned_close_finishes_same_cleanup_before_propagating():
    with private_tempdir() as tmp:
        now = [0.0]

        class SlowTunnel(FakeTunnel):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.exit_started = asyncio.Event()
                self.release_exit = asyncio.Event()
                self.exit_calls = 0

            async def __aexit__(self, *_args):
                self.exit_calls += 1
                self.exit_started.set()
                await self.release_exit.wait()
                self.closed = True

        tunnel = SlowTunnel(serial="TEST-SERIAL")
        dvt = FakeContext(tunnel.rsd)
        screenshot = FakeScreenshot(dvt, [])

        async def scenario():
            provider = capture.USBScreenCapture(
                serial="TEST-SERIAL",
                target=target_for("TEST-SERIAL"),
                data_root=capture.TASK_DATA_ROOT,
                output_directory=Path(tmp) / "usb-frames",
                tunnel_factory=lambda **_kwargs: tunnel,
                dvt_factory=lambda _rsd: dvt,
                screenshot_factory=lambda _dvt: screenshot,
                dependency_verifier=lambda: True,
                clock=lambda: now[0],
                session_timeout=1,
            )
            await provider.open()
            now[0] = 1
            capture_task = asyncio.create_task(provider.capture())
            await asyncio.wait_for(tunnel.exit_started.wait(), timeout=2)
            capture_task.cancel()
            await asyncio.sleep(0)
            assert not capture_task.done(), "cancel must wait for provider-owned teardown"
            tunnel.release_exit.set()
            with pytest.raises(asyncio.CancelledError):
                await capture_task

            assert provider.cleanup_status == "CLOSED"
            assert tunnel.closed and dvt.closed and screenshot.closed
            await provider.close()
            assert tunnel.exit_calls == 1

        asyncio.run(scenario())


def test_failed_provider_teardown_remains_failed_on_repeated_close():
    with private_tempdir() as tmp:
        class FailingTunnel(FakeTunnel):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.exit_calls = 0

            async def __aexit__(self, *_args):
                self.exit_calls += 1
                raise OSError("private synthetic teardown failure")

        tunnel = FailingTunnel(serial="TEST-SERIAL")
        dvt = FakeContext(tunnel.rsd)
        screenshot = FakeScreenshot(dvt, [])

        async def scenario():
            provider = capture.USBScreenCapture(
                serial="TEST-SERIAL",
                target=target_for("TEST-SERIAL"),
                data_root=capture.TASK_DATA_ROOT,
                output_directory=Path(tmp) / "usb-frames",
                tunnel_factory=lambda **_kwargs: tunnel,
                dvt_factory=lambda _rsd: dvt,
                screenshot_factory=lambda _dvt: screenshot,
                dependency_verifier=lambda: True,
                clock=lambda: 1.0,
                session_timeout=1,
            )
            await provider.open()
            with pytest.raises(capture.CaptureError, match="teardown failed"):
                await provider.capture()
            assert provider.cleanup_status == "FAILED"
            with pytest.raises(capture.CaptureError, match="teardown failed"):
                await provider.close()
            assert tunnel.exit_calls == 1

        asyncio.run(scenario())


def test_frame_limit_closes_resources_and_invalid_png_is_rejected():
    with private_tempdir() as tmp:
        tunnel = FakeTunnel(serial="TEST-SERIAL")
        dvt = FakeContext(tunnel.rsd)
        screenshot = FakeScreenshot(dvt, [png_bytes(), png_bytes(), png_bytes()])

        async def scenario():
            provider = capture.USBScreenCapture(
                serial="TEST-SERIAL",
                target=target_for("TEST-SERIAL"),
                data_root=capture.TASK_DATA_ROOT,
                output_directory=Path(tmp) / "usb-frames",
                tunnel_factory=lambda **_kwargs: tunnel,
                dvt_factory=lambda _rsd: dvt,
                screenshot_factory=lambda _dvt: screenshot,
                dependency_verifier=lambda: True,
                ring_size=2,
                max_frames=2,
            )
            await provider.open()
            await provider.capture()
            await provider.capture()
            with pytest.raises(capture.CaptureError, match="frame limit"):
                await provider.capture()
            assert provider.cleanup_status == "CLOSED"
            assert tunnel.closed and dvt.closed and screenshot.closed

            bad_tunnel = FakeTunnel(serial="TEST-SERIAL")
            bad_dvt = FakeContext(bad_tunnel.rsd)
            bad_screenshot = FakeScreenshot(bad_dvt, [b"not a PNG"])
            bad_provider = capture.USBScreenCapture(
                serial="TEST-SERIAL",
                target=target_for("TEST-SERIAL"),
                data_root=capture.TASK_DATA_ROOT,
                output_directory=Path(tmp) / "bad-frames",
                tunnel_factory=lambda **_kwargs: bad_tunnel,
                dvt_factory=lambda _rsd: bad_dvt,
                screenshot_factory=lambda _dvt: bad_screenshot,
                dependency_verifier=lambda: True,
            )
            await bad_provider.open()
            with pytest.raises(capture.CaptureError, match="valid PNG"):
                await bad_provider.capture()
            assert bad_provider.cleanup_status == "CLOSED"
            assert bad_tunnel.closed and bad_dvt.closed and bad_screenshot.closed

        asyncio.run(scenario())


def test_screenshot_timeout_closes_resources():
    with private_tempdir() as tmp:
        tunnel = FakeTunnel(serial="TEST-SERIAL")
        dvt = FakeContext(tunnel.rsd)

        class SlowScreenshot(FakeScreenshot):
            async def get_screenshot(self):
                await asyncio.sleep(0.05)
                return png_bytes()

        screenshot = SlowScreenshot(dvt, [])

        async def scenario():
            provider = capture.USBScreenCapture(
                serial="TEST-SERIAL",
                target=target_for("TEST-SERIAL"),
                data_root=capture.TASK_DATA_ROOT,
                output_directory=Path(tmp) / "slow-frames",
                tunnel_factory=lambda **_kwargs: tunnel,
                dvt_factory=lambda _rsd: dvt,
                screenshot_factory=lambda _dvt: screenshot,
                dependency_verifier=lambda: True,
                screenshot_timeout=0.01,
            )
            await provider.open()
            with pytest.raises(capture.CaptureError, match="request failed"):
                await provider.capture()
            assert provider.cleanup_status == "CLOSED"
            assert tunnel.closed and dvt.closed and screenshot.closed

        asyncio.run(scenario())


def test_tunnel_source_hash_check_fails_closed(tmp_path):
    source = tmp_path / "userspace_tunnel.py"
    pristine = tmp_path / "userspace_tunnel.py.phonelab-pristine"
    source.write_bytes(b"reviewed")
    pristine.write_bytes(b"original")
    assert capture.validate_tunnel_source(
        source,
        pristine,
        hashlib.sha256(b"reviewed").hexdigest(),
        hashlib.sha256(b"original").hexdigest(),
    )
    with pytest.raises(capture.CaptureError):
        capture.validate_tunnel_source(source, pristine, "0" * 64, hashlib.sha256(b"original").hexdigest())
