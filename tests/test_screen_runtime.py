import asyncio
import io
import json
import logging
import threading
import time
from pathlib import Path
from dataclasses import replace

from phonelab.control import Frame
from phonelab.screen_runtime import ScreenCaptureRuntime
from phonelab.usb_capture import CaptureError


PNG = b"\x89PNG\r\n\x1a\nfixture"


class FakeProvider:
    def __init__(self, *, serial, target, data_root, output_directory):
        self.serial = serial
        self.target = target
        self.output_directory = Path(output_directory)
        self.output_directory.mkdir(parents=True, exist_ok=True)
        self.path = self.output_directory / "frame.png"
        self.path.write_bytes(PNG)
        self.calls = 0
        self.closed = 0
        self.generation = "fake-generation"
        self.fail = threading.Event()
        self.fail_open = False
        self.fail_close = False
        self.frame_clock = time.monotonic
        self.frame_offset = 0.0
        self.block_after_first = False
        self.capture_started = threading.Event()
        self.release_capture = threading.Event()

    async def open(self):
        if self.fail_open:
            raise OSError("private fake open failure")
        return None

    async def capture(self):
        self.calls += 1
        if self.calls > 1 and self.block_after_first:
            self.capture_started.set()
            try:
                await asyncio.to_thread(self.release_capture.wait)
            except asyncio.CancelledError:
                await asyncio.to_thread(self.release_capture.wait)
        if self.fail.is_set():
            raise OSError("private fake failure")
        return {
            "frame": Frame(self.target, self.calls, self.frame_clock() + self.frame_offset, 828, 1792, "portrait", None,
                           "USB_SCREENSHOT", None),
            "private_path": self.path,
            "image_bytes": PNG,
            "generation": self.generation,
        }

    async def close(self):
        self.closed += 1
        if self.fail_close:
            raise OSError("private fake close failure")


def make_runtime(tmp_path, monkeypatch, *, clock=None, fail_open=False):
    private = tmp_path / "private"
    private.mkdir()
    (private / "target.json").write_text(json.dumps({
        "udid": "0000000000000000000000000000000000000001", "target": "iphone-ac39233f6b70"
    }))
    providers = []

    def factory(**kwargs):
        provider = FakeProvider(**kwargs)
        provider.fail_open = fail_open
        provider.frame_clock = clock or time.monotonic
        providers.append(provider)
        return provider

    runtime = ScreenCaptureRuntime(tmp_path, provider_factory=factory, clock=clock, interval=0.03)
    return runtime, providers


def test_usb_capture_starts_only_explicitly_and_publishes_private_png_metadata(tmp_path):
    runtime, providers = make_runtime(tmp_path, None)
    assert runtime.status()["state"] == "UNKNOWN"
    assert providers == []
    runtime.start()
    try:
        image = runtime.screen()
        assert image == PNG
        metadata = runtime.status()
        assert {key: value for key, value in metadata.items() if key != "received_monotonic"} == {
            "state": "fresh", "target": "iphone-ac39233f6b70", "sequence": 1,
            "width": 828, "height": 1792, "orientation": "portrait",
            "source": "USB_SCREENSHOT", "source_time": None,
            "locked": "UNKNOWN", "fresh": True,
        }
        assert isinstance(metadata["received_monotonic"], float)
        assert providers[0].serial == "0000000000000000000000000000000000000001"
        assert providers[0].serial not in json.dumps(metadata)
        assert str(tmp_path) not in json.dumps(metadata)
    finally:
        runtime.close()
    assert providers[0].closed == 1


def test_capture_socket_loop_is_private_selector_and_preserves_global_policy(tmp_path):
    policy = asyncio.get_event_loop_policy()
    loops = []
    class LoopProvider(FakeProvider):
        async def open(self):
            loops.append(asyncio.get_running_loop())
    runtime, providers = make_runtime(tmp_path, None)
    runtime._provider_factory = lambda **kwargs: _record_provider(providers, LoopProvider, kwargs)
    runtime.start()
    runtime.close()
    assert len(loops) == 1
    assert isinstance(loops[0], asyncio.SelectorEventLoop)
    assert loops[0].is_closed()
    assert asyncio.get_event_loop_policy() is policy


def test_stale_capture_is_not_returned(tmp_path):
    now = [10.0]
    runtime, _ = make_runtime(tmp_path, None, clock=lambda: now[0])
    runtime.start()
    try:
        assert runtime.screen() == PNG
        now[0] = 13.0
        assert runtime.screen() is None
        assert runtime.status()["fresh"] is False
    finally:
        runtime.close()


def test_stale_provider_timestamp_is_not_refreshed_by_manager(tmp_path):
    now = [100.0]
    runtime, providers = make_runtime(tmp_path, None, clock=lambda: now[0])
    providers_factory = runtime._provider_factory
    def stale_factory(**kwargs):
        provider = providers_factory(**kwargs)
        provider.frame_offset = -3.0
        return provider
    runtime._provider_factory = stale_factory
    try:
        runtime.start()
    except RuntimeError as exc:
        assert "failed before its first frame" in str(exc)
    else:
        raise AssertionError("stale provider frame must not start capture")
    assert runtime.screen() is None


def test_future_provider_timestamp_is_rejected(tmp_path):
    now = [100.0]
    runtime, providers = make_runtime(tmp_path, None, clock=lambda: now[0])
    original_factory = runtime._provider_factory
    def future_factory(**kwargs):
        provider = original_factory(**kwargs)
        provider.frame_offset = 0.25
        return provider
    runtime._provider_factory = future_factory
    try:
        runtime.start()
    except RuntimeError as exc:
        assert "failed before its first frame" in str(exc)
    else:
        raise AssertionError("future provider frame must not start capture")
    assert runtime.screen() is None


def test_frame_validation_failures_use_fixed_private_reason_codes(tmp_path):
    cases = [
        ("stale", "FRAME_AGE_STALE"),
        ("future", "FRAME_AGE_FUTURE"),
        ("target", "FRAME_TARGET_INVALID"),
        ("sequence", "FRAME_SEQUENCE_INVALID"),
        ("generation", "FRAME_GENERATION_INVALID"),
        ("path", "FRAME_PATH_INVALID"),
    ]
    for mutation, expected in cases:
        case_root = tmp_path / mutation
        case_root.mkdir()
        runtime, providers = make_runtime(case_root, None, clock=lambda: 100.0)

        class InvalidFrameProvider(FakeProvider):
            async def capture(self):
                result = await super().capture()
                frame = result["frame"]
                if mutation == "stale":
                    result["frame"] = replace(frame, received_monotonic=97.0)
                elif mutation == "future":
                    result["frame"] = replace(frame, received_monotonic=100.25)
                elif mutation == "target":
                    result["frame"] = replace(frame, target="private-target-value")
                elif mutation == "sequence":
                    result["frame"] = replace(frame, sequence=0)
                elif mutation == "generation":
                    if self.calls > 1:
                        result["generation"] = "private-generation-value"
                elif mutation == "path":
                    result["private_path"] = case_root / "outside-private.png"
                return result

        def invalid_factory(**kwargs):
            provider = _record_provider(providers, InvalidFrameProvider, kwargs)
            provider.frame_clock = lambda: 100.0
            return provider
        runtime._provider_factory = invalid_factory
        try:
            if mutation == "generation":
                runtime.start()
                deadline = time.monotonic() + 2
                while runtime.status()["state"] != "failed" and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert runtime.status()["state"] == "failed"
            else:
                try:
                    runtime.start()
                except RuntimeError as exc:
                    assert "failed before its first frame" in str(exc)
                else:
                    raise AssertionError(f"invalid frame must fail before capture starts: {mutation}")
            status = runtime.shutdown_status()
            assert status["failure_code"] == expected, mutation
            records = list((case_root / "private").glob("capture-failure-*.json"))
            assert len(records) == 1
            content = records[0].read_text(encoding="utf-8")
            assert json.loads(content)["failure_code"] == expected
            assert "private-target-value" not in content
            assert "private-generation-value" not in content
            assert str(case_root) not in content
        finally:
            runtime.close()


def test_stale_frame_is_dropped_then_same_provider_can_publish_fresh_frame(tmp_path):
    class FreshStaleFreshProvider(FakeProvider):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.third_started = threading.Event()
            self.release_third = threading.Event()

        async def capture(self):
            if self.calls == 2:
                self.third_started.set()
                await asyncio.to_thread(self.release_third.wait)
            result = await super().capture()
            if self.calls == 2:
                result["frame"] = replace(result["frame"], received_monotonic=97.0)
            return result

    now = lambda: 100.0
    runtime, providers = make_runtime(tmp_path, None, clock=now)
    def factory(**kwargs):
        provider = _record_provider(providers, FreshStaleFreshProvider, kwargs)
        provider.frame_clock = now
        return provider
    runtime._provider_factory = factory
    runtime.start()
    provider = providers[0]
    try:
        assert provider.third_started.wait(2)
        assert runtime.screen() is None
        status = runtime.shutdown_status()
        assert status["frame_drop_code"] == "FRAME_AGE_STALE"
        assert status["frame_drop_count"] == 1
        assert status["consecutive_stale_frames"] == 1
        provider.release_third.set()
        deadline = time.monotonic() + 2
        while runtime.screen() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runtime.screen() == PNG
        assert len(providers) == 1
        assert runtime.shutdown_status()["consecutive_stale_frames"] == 0
    finally:
        provider.release_third.set()
        runtime.close()


def test_stale_frame_cannot_hide_sequence_or_generation_regression(tmp_path):
    for mutation, expected in (("sequence", "FRAME_SEQUENCE_INVALID"),
                               ("generation", "FRAME_GENERATION_INVALID")):
        case_root = tmp_path / mutation
        case_root.mkdir()
        runtime, providers = make_runtime(case_root, None, clock=lambda: 100.0)

        class RegressionProvider(FakeProvider):
            async def capture(self):
                result = await super().capture()
                if self.calls == 2:
                    frame = result["frame"]
                    result["frame"] = replace(frame, received_monotonic=97.0,
                                               sequence=1 if mutation == "sequence" else frame.sequence)
                    if mutation == "generation":
                        result["generation"] = "new-private-generation"
                return result

        def factory(**kwargs):
            provider = _record_provider(providers, RegressionProvider, kwargs)
            provider.frame_clock = lambda: 100.0
            return provider
        runtime._provider_factory = factory
        runtime.start()
        deadline = time.monotonic() + 2
        while runtime.status()["state"] != "failed" and time.monotonic() < deadline:
            time.sleep(0.01)
        try:
            assert runtime.status()["state"] == "failed"
            assert runtime.shutdown_status()["failure_code"] == expected
            assert len(providers) == 1
            assert providers[0].closed == 1
        finally:
            runtime.close()


def test_three_consecutive_stale_frames_fail_and_close_once(tmp_path):
    now = lambda: 100.0
    runtime, providers = make_runtime(tmp_path, None, clock=now)

    class ThreeStaleProvider(FakeProvider):
        async def capture(self):
            result = await super().capture()
            if self.calls > 1:
                result["frame"] = replace(result["frame"], received_monotonic=97.0)
            return result

    def factory(**kwargs):
        provider = _record_provider(providers, ThreeStaleProvider, kwargs)
        provider.frame_clock = now
        return provider
    runtime._provider_factory = factory
    runtime.start()
    deadline = time.monotonic() + 2
    while runtime.status()["state"] != "failed" and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        status = runtime.shutdown_status()
        assert runtime.status()["state"] == "failed"
        assert status["frame_drop_count"] == 3
        assert status["consecutive_stale_frames"] == 3
        assert status["failure_code"] == "FRAME_AGE_STALE"
        assert len(providers) == 1
        assert providers[0].closed == 1
        record = json.loads(next((tmp_path / "private").glob("capture-failure-*.json")).read_text())
        assert record["frame_drop_count"] == 3
        assert record["frame_drop_code"] == "FRAME_AGE_STALE"
    finally:
        runtime.close()


def test_capture_failure_clears_frame_closes_provider_and_does_not_reconnect(tmp_path):
    runtime, providers = make_runtime(tmp_path, None)
    runtime.start()
    try:
        assert runtime.screen() == PNG
        provider = providers[0]
        provider.fail.set()
        deadline = time.monotonic() + 2
        while runtime.status()["state"] != "failed" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runtime.status()["state"] == "failed"
        assert runtime.screen() is None
        assert len(providers) == 1
        assert provider.closed == 1
        diagnostic = runtime.shutdown_status()
        assert diagnostic["failure_code"] == "OS_ERROR"
        assert diagnostic["failure_phase"] == "CAPTURE"
        records = list((tmp_path / "private").glob("capture-failure-*.json"))
        assert len(records) == 1
        record = json.loads(records[0].read_text(encoding="utf-8"))
        assert record == {"failure_code": "OS_ERROR", "failure_phase": "CAPTURE",
                          "async_failed": False, "cleanup_failed": False,
                          "frame_drop_code": "NONE", "frame_drop_count": 0,
                          "consecutive_stale_frames": 0}
        assert "private fake failure" not in records[0].read_text(encoding="utf-8")
    finally:
        runtime.close()


def test_capture_error_is_classified_without_persisting_its_message(tmp_path):
    class CaptureErrorProvider(FakeProvider):
        async def capture(self):
            if self.calls:
                raise CaptureError("PRIVATE_RSD_DIAGNOSTIC")
            return await super().capture()

    runtime, providers = make_runtime(tmp_path, None)
    runtime._provider_factory = lambda **kwargs: _record_provider(providers, CaptureErrorProvider, kwargs)
    runtime.start()
    deadline = time.monotonic() + 2
    while runtime.status()["state"] != "failed" and time.monotonic() < deadline:
        time.sleep(0.01)
    diagnostic = runtime.shutdown_status()
    assert diagnostic["failure_code"] == "CAPTURE_ERROR"
    assert diagnostic["failure_phase"] == "CAPTURE"
    records = list((tmp_path / "private").glob("capture-failure-*.json"))
    assert len(records) == 1
    assert "PRIVATE_RSD_DIAGNOSTIC" not in records[0].read_text(encoding="utf-8")
    runtime.close()


def test_new_capture_generation_invalidates_and_closes_without_reopen(tmp_path):
    runtime, providers = make_runtime(tmp_path, None)
    runtime.start()
    try:
        assert runtime.screen() == PNG
        provider = providers[0]
        provider.generation = "unexpected-generation"
        deadline = time.monotonic() + 2
        while runtime.status()["state"] != "failed" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runtime.status()["state"] == "failed"
        assert runtime.screen() is None
        assert len(providers) == 1
        assert provider.closed == 1
    finally:
        runtime.close()


def test_missing_or_malformed_private_target_binding_fails_before_provider_open(tmp_path):
    (tmp_path / "private").mkdir()
    runtime = ScreenCaptureRuntime(tmp_path, provider_factory=lambda **_: (_ for _ in ()).throw(AssertionError("opened")))
    try:
        try:
            runtime.start()
        except ValueError as exc:
            assert "target binding" in str(exc)
        else:
            raise AssertionError("missing target binding must fail closed")
    finally:
        runtime.close()


def test_private_target_hash_must_match_serial(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    (private / "target.json").write_text(json.dumps({
        "udid": "0000000000000000000000000000000000000001", "target": "iphone-000000000000"
    }))
    runtime = ScreenCaptureRuntime(tmp_path, provider_factory=lambda **_: (_ for _ in ()).throw(AssertionError("opened")))
    try:
        try:
            runtime.start()
        except ValueError as exc:
            assert "target binding" in str(exc)
        else:
            raise AssertionError("mismatched private target must fail closed")
    finally:
        runtime.close()


def test_open_failure_closes_owned_provider_without_retry(tmp_path):
    runtime, providers = make_runtime(tmp_path, None, fail_open=True)
    try:
        runtime.start()
    except RuntimeError as exc:
        assert "failed before its first frame" in str(exc)
    else:
        raise AssertionError("failed open must not report a started capture")
    assert len(providers) == 1
    assert providers[0].closed == 1
    assert runtime.screen() is None
    assert runtime.shutdown_status()["failure_code"] == "OS_ERROR"
    assert runtime.shutdown_status()["failure_phase"] == "OPEN"


def test_cleanup_failure_is_reported_and_not_marked_stopped(tmp_path):
    runtime, providers = make_runtime(tmp_path, None)
    runtime.start()
    providers[0].fail_close = True
    try:
        runtime.close()
    except RuntimeError as exc:
        assert "cleanup failed" in str(exc)
    else:
        raise AssertionError("failed provider teardown must be surfaced")
    assert runtime.status()["state"] == "failed"
    assert runtime.screen() is None
    assert runtime.shutdown_status()["failure_code"] == "OS_ERROR"
    assert runtime.shutdown_status()["failure_phase"] == "PROVIDER_CLOSE"
    records = list((tmp_path / "private").glob("capture-failure-*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text(encoding="utf-8"))["cleanup_failed"] is True


def test_capture_finishing_during_stop_is_never_published(tmp_path):
    runtime, providers = make_runtime(tmp_path, None)
    runtime.start()
    provider = providers[0]
    provider.block_after_first = True
    assert provider.capture_started.wait(2)
    errors = []
    closer = threading.Thread(target=lambda: _close_capture(runtime, errors))
    closer.start()
    time.sleep(0.05)
    provider.release_capture.set()
    closer.join(timeout=3)
    assert not closer.is_alive()
    assert errors == []
    assert runtime.screen() is None
    assert runtime.status()["state"] == "stopped"
    assert provider.closed == 1


def test_concurrent_close_waiters_do_not_cancel_provider_teardown(tmp_path):
    teardown_started = threading.Event()
    release_teardown = threading.Event()

    class SlowCloseProvider(FakeProvider):
        async def close(self):
            self.closed += 1
            teardown_started.set()
            await asyncio.to_thread(release_teardown.wait)

    runtime, providers = make_runtime(tmp_path, None)
    runtime._provider_factory = lambda **kwargs: _record_provider(providers, SlowCloseProvider, kwargs)
    runtime.start()
    errors = []
    first = threading.Thread(target=lambda: _close_capture(runtime, errors))
    second = threading.Thread(target=lambda: _close_capture(runtime, errors))
    try:
        first.start()
        assert teardown_started.wait(2)
        second.start()
        # Ensure the second close has time to reach the shared owner while its
        # teardown await is pending, then allow any surviving cleanup to finish.
        time.sleep(0.05)
        release_teardown.set()
        first.join(timeout=3)
        second.join(timeout=3)
        assert not first.is_alive()
        assert not second.is_alive()
        assert errors == []
        assert providers[0].closed == 1
        assert runtime.status()["state"] == "stopped"
        assert runtime.screen() is None
    finally:
        release_teardown.set()
        first.join(timeout=3)
        if second.ident is not None:
            second.join(timeout=3)


def test_async_callback_failure_during_teardown_preserves_cleanup_and_surfaces_failure(
        tmp_path, capsys, caplog):
    teardown_started = threading.Event()
    release_teardown = threading.Event()
    teardown_completed = threading.Event()

    class CallbackDuringCloseProvider(FakeProvider):
        async def close(self):
            self.closed += 1

            def fail_callback():
                raise RuntimeError("PRIVATE_ASYNC_TEARDOWN_CONTEXT")

            asyncio.get_running_loop().call_soon(fail_callback)
            teardown_started.set()
            await asyncio.to_thread(release_teardown.wait)
            teardown_completed.set()

    runtime, providers = make_runtime(tmp_path, None)
    runtime._provider_factory = lambda **kwargs: _record_provider(
        providers, CallbackDuringCloseProvider, kwargs)
    runtime.start()
    errors = []
    closer = threading.Thread(target=lambda: _close_capture(runtime, errors))
    try:
        closer.start()
        assert teardown_started.wait(2)
        time.sleep(0.05)  # let the loop deliver the callback error during teardown
        assert not teardown_completed.is_set()
        release_teardown.set()
        closer.join(timeout=3)
        assert not closer.is_alive()
        assert errors == ["RuntimeError"]
        assert teardown_completed.is_set()
        assert providers[0].closed == 1
        assert runtime.status()["state"] == "failed"
        assert runtime.screen() is None
        captured = capsys.readouterr()
        assert "PRIVATE_ASYNC_TEARDOWN_CONTEXT" not in captured.out + captured.err
        assert "PRIVATE_ASYNC_TEARDOWN_CONTEXT" not in caplog.text
    finally:
        release_teardown.set()
        closer.join(timeout=3)


def _close_capture(runtime, errors):
    try:
        runtime.close()
    except Exception as exc:
        errors.append(type(exc).__name__)


def test_dependency_logguard_suppresses_private_logger_output_and_restores_state(tmp_path, caplog):
    logger = logging.getLogger("pymobiledevice3.screen_runtime_privacy_test")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    old_handlers, old_level, old_propagate = list(logger.handlers), logger.level, logger.propagate
    logger.handlers[:] = [handler]
    logger.setLevel(logging.WARNING)
    logger.propagate = True

    class LoggingProvider(FakeProvider):
        async def capture(self):
            logger.warning("PRIVATE_DEVICE_SECRET")
            return await super().capture()

    runtime, providers = make_runtime(tmp_path, None)
    runtime._provider_factory = lambda **kwargs: _record_provider(providers, LoggingProvider, kwargs)
    try:
        runtime.start()
        assert "PRIVATE_DEVICE_SECRET" not in caplog.text
        assert "PRIVATE_DEVICE_SECRET" not in stream.getvalue()
        runtime.close()
        assert logger.handlers == [handler]
        assert logger.level == logging.WARNING
        assert logger.propagate is True
        assert "PRIVATE_DEVICE_SECRET" not in caplog.text
    finally:
        try:
            runtime.close()
        except RuntimeError:
            pass
        logger.handlers[:] = old_handlers
        logger.setLevel(old_level)
        logger.propagate = old_propagate


def test_pending_dependency_task_cancellation_stays_guarded_until_loop_drain(tmp_path, capsys, caplog):
    logger = logging.getLogger("pymobiledevice3.screen_runtime_pending_task_test")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    old_handlers, old_level, old_propagate = list(logger.handlers), logger.level, logger.propagate
    logger.handlers[:] = [handler]
    logger.setLevel(logging.WARNING)
    logger.propagate = True

    class PendingTaskProvider(FakeProvider):
        async def open(self):
            self.background_ready = asyncio.Event()

            async def wait_until_cancelled():
                self.background_ready.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    logger.warning("PRIVATE_PENDING_TASK_SECRET")

            self.background_task = asyncio.create_task(wait_until_cancelled())

        async def capture(self):
            await self.background_ready.wait()
            return await super().capture()

    runtime, providers = make_runtime(tmp_path, None)
    runtime._provider_factory = lambda **kwargs: _record_provider(providers, PendingTaskProvider, kwargs)
    try:
        runtime.start()
        runtime.close()
        assert providers[0].background_task.cancelled()
        assert "PRIVATE_PENDING_TASK_SECRET" not in stream.getvalue()
        assert "PRIVATE_PENDING_TASK_SECRET" not in caplog.text
        captured = capsys.readouterr()
        assert "PRIVATE_PENDING_TASK_SECRET" not in captured.out + captured.err
        assert logger.handlers == [handler]
        assert logger.level == logging.WARNING
        assert logger.propagate is True
    finally:
        try:
            runtime.close()
        except RuntimeError:
            pass
        logger.handlers[:] = old_handlers
        logger.setLevel(old_level)
        logger.propagate = old_propagate


def _record_provider(providers, provider_type, kwargs):
    provider = provider_type(**kwargs)
    provider.frame_clock = time.monotonic
    providers.append(provider)
    return provider


def test_unhandled_async_callback_is_sanitized_and_marks_capture_failed(tmp_path, capsys, caplog):
    class CallbackProvider(FakeProvider):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.close_started = threading.Event()
            self.allow_close = threading.Event()

        async def open(self):
            def fail_callback():
                raise RuntimeError("PRIVATE_DEVICE_SECRET_FROM_ASYNC_CONTEXT")
            asyncio.get_running_loop().call_soon(fail_callback)

        async def close(self):
            self.close_started.set()
            try:
                await asyncio.to_thread(self.allow_close.wait)
            finally:
                self.closed += 1

    runtime, providers = make_runtime(tmp_path, None)
    runtime._provider_factory = lambda **kwargs: _record_provider(providers, CallbackProvider, kwargs)
    try:
        try:
            runtime.start()
        except RuntimeError:
            pass
        assert providers[0].close_started.wait(2)
        captured = capsys.readouterr()
        assert "PRIVATE_DEVICE_SECRET_FROM_ASYNC_CONTEXT" not in captured.out + captured.err
        assert "PRIVATE_DEVICE_SECRET_FROM_ASYNC_CONTEXT" not in caplog.text
        assert runtime.status()["state"] == "failed"
        assert runtime.screen() is None
        assert len(providers) == 1
        assert providers[0].closed == 0
        diagnostic = runtime.shutdown_status()
        assert diagnostic['async_failure_codes'] == ['RUNTIME_ERROR']
        assert diagnostic['async_failure_phases'] == ['CAPTURE']
        assert 'PRIVATE_DEVICE_SECRET_FROM_ASYNC_CONTEXT' not in json.dumps(diagnostic)

        close_started = threading.Event()
        close_done = threading.Event()
        close_errors = []

        def close_runtime():
            close_started.set()
            try:
                runtime.close()
            except RuntimeError as exc:
                close_errors.append(exc)
            finally:
                close_done.set()

        close_thread = threading.Thread(target=close_runtime)
        close_thread.start()
        assert close_started.wait(1)
        assert not close_done.is_set()
        providers[0].allow_close.set()
        assert close_done.wait(3)
        close_thread.join(timeout=1)
        assert not close_thread.is_alive()
        assert len(close_errors) == 1
        assert type(close_errors[0]) is RuntimeError
        diagnostic = runtime.shutdown_status()
        assert providers[0].closed == 1
        assert diagnostic['owner_thread_alive'] is False
        assert diagnostic['provider_cleanup'] == 'UNKNOWN'
        assert diagnostic['async_failure_codes'] == ['RUNTIME_ERROR']
        assert diagnostic['async_failure_phases'] == ['CAPTURE']
        assert 'PRIVATE_DEVICE_SECRET_FROM_ASYNC_CONTEXT' not in json.dumps(diagnostic)

        captured = capsys.readouterr()
        assert "PRIVATE_DEVICE_SECRET_FROM_ASYNC_CONTEXT" not in captured.out + captured.err
        assert "PRIVATE_DEVICE_SECRET_FROM_ASYNC_CONTEXT" not in caplog.text
    finally:
        if providers:
            providers[0].allow_close.set()
        try:
            runtime.close()
        except RuntimeError:
            pass
