"""Native regressions for cancellation recovery around the portable core.

The harness compiles the real ESP32KeyBridge.cpp and injects the current
production BLE output adapter at a marker in the fixture. No adapter copy is
kept in the test fixture.
"""

from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

from phonelab.paths import resolve_data_root


ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT / "firmware/libraries/ESP32KeyBridge/src"
ADAPTER_HEADER = CORE_SRC / "ESP32KeyBridgeEspBle.h"
FIXTURE = ROOT / "tests/fixtures/core_cancel_recovery.cpp"
CLASS_START = "class EspBleHidOutputAdapter : public OutputAdapter"
CLASS_END = "// Input: BLE HID host (central)"
MARKER = "/*__PRODUCTION_OUTPUT_ADAPTER__*/"
PROJECT_GXX = (
    resolve_data_root()
    / "airplay-build"
    / "msys2"
    / "msys64"
    / "ucrt64"
    / "bin"
    / "g++.exe"
)


def host_cpp_compiler():
    configured = os.environ.get("PHONELAB_CXX")
    if configured:
        return configured if Path(configured).is_file() else shutil.which(configured)
    return shutil.which("g++") or shutil.which("clang++") or (
        str(PROJECT_GXX) if PROJECT_GXX.is_file() else None
    )


class CoreCancelRecoveryNativeTests(unittest.TestCase):
    @unittest.skipUnless(host_cpp_compiler(), "host C++ compiler unavailable")
    def test_actual_core_and_production_adapter_cancel_recovery(self):
        adapter_source = ADAPTER_HEADER.read_text(encoding="utf-8")
        start = adapter_source.index(CLASS_START)
        end = adapter_source.index(CLASS_END, start)
        production_adapter = adapter_source[start:end]

        fixture_source = FIXTURE.read_text(encoding="utf-8")
        self.assertEqual(fixture_source.count(MARKER), 1)
        harness_source = fixture_source.replace(MARKER, production_adapter)

        compiler = host_cpp_compiler()
        with tempfile.TemporaryDirectory(prefix="core-cancel-recovery-") as temp:
            temp_path = Path(temp)
            cpp = temp_path / "core_cancel_recovery.cpp"
            exe = temp_path / "core_cancel_recovery.exe"
            cpp.write_text(harness_source, encoding="utf-8")
            child_env = os.environ.copy()
            compiler_path = Path(compiler).resolve()
            child_env["PATH"] = str(compiler_path.parent) + os.pathsep + child_env.get("PATH", "")

            compile_result = subprocess.run(
                [
                    compiler,
                    "-std=c++17",
                    "-I",
                    str(CORE_SRC),
                    str(cpp),
                    str(CORE_SRC / "ESP32KeyBridge.cpp"),
                    "-o",
                    str(exe),
                ],
                capture_output=True,
                text=True,
                env=child_env,
            )
            self.assertEqual(
                compile_result.returncode,
                0,
                compile_result.stdout + compile_result.stderr,
            )
            run_result = subprocess.run(
                [str(exe)], capture_output=True, text=True, env=child_env
            )
            self.assertEqual(run_result.returncode, 0, run_result.stdout + run_result.stderr)


if __name__ == "__main__":
    unittest.main()
