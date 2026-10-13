"""Safety contract tests for the BLE output adapter.

The native test extracts and compiles the actual output-adapter class from the
production header against small fake BLE endpoints. It is skipped when no host
C++ compiler is installed; the project ESP32 cross-compiler cannot execute a
native harness.
"""

from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest
from phonelab.paths import resolve_data_root


ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "firmware/libraries/ESP32KeyBridge/src/ESP32KeyBridgeEspBle.h"
CLASS_START = "class EspBleHidOutputAdapter : public OutputAdapter"
CLASS_END = "// Input: BLE HID host (central)"
PROJECT_GXX = resolve_data_root() / "airplay-build" / "msys2" / "msys64" / "ucrt64" / "bin" / "g++.exe"


def host_cpp_compiler():
    configured = os.environ.get("PHONELAB_CXX")
    if configured:
        return configured if Path(configured).is_file() else shutil.which(configured)
    return shutil.which("g++") or shutil.which("clang++") or (str(PROJECT_GXX) if PROJECT_GXX.is_file() else None)


class BleOutputSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = HEADER.read_text(encoding="utf-8")
        start = cls.source.index(CLASS_START)
        end = cls.source.index(CLASS_END, start)
        cls.output_class = cls.source[start:end]

    def test_mouse_readiness_discard_and_failure_counter_are_explicit(self):
        self.assertIn("bool mouseReady() const { return ble_.hidMouse().ready(); }", self.output_class)
        self.assertIn("uint32_t mouseSendFailureCount() const { return mouseSendFailureCount_; }", self.output_class)
        self.assertIn("void discardPendingMouseMotion()", self.output_class)
        discard = self.output_class[
            self.output_class.index("void discardPendingMouseMotion()"):
            self.output_class.index("// LED output reports", self.output_class.index("void discardPendingMouseMotion()"))
        ]
        self.assertIn("pendingAxis_[axis] = 0", discard)
        self.assertNotIn("sendReport", discard)
        fail_start = self.output_class.index("else\n    {\n      // Not sent: put the movement back")
        fail_end = self.output_class.index("    }", fail_start)
        failure_path = self.output_class[fail_start:fail_end]
        self.assertIn("++mouseSendFailureCount_", failure_path)
        self.assertIn("pendingAxis_[static_cast<size_t>(Axis::X)] += report.x", failure_path)

    @unittest.skipUnless(host_cpp_compiler(), "host C++ compiler unavailable")
    def test_native_behavior_distinguishes_readiness_and_drops_discarded_delta(self):
        """Run the production class, not a reimplemented Python model."""
        prelude = r'''
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <cassert>
namespace esp32keybridge {
enum class Axis : uint8_t { X=0, Y=1, Wheel=2, Pan=3 };
constexpr size_t kAxisCount=4;
struct LockState { bool numLock=false, capsLock=false, scrollLock=false, kana=false; };
struct EspBleHidKeyboardConfig {}; struct EspBleHidMouseConfig {}; struct EspBleHidConsumerControlConfig {};
struct EspBleHidKeyboardOutputReport { bool numLock=false,capsLock=false,scrollLock=false,kana=false; };
struct EspBleHidKeyboardReport { uint8_t modifiers=0, keys[6]={}; };
struct EspBleHidMouseReport { uint8_t buttons=0; int8_t x=0,y=0,wheel=0; };
struct HidKeyboardReport { static constexpr size_t MaxKeys=6; uint8_t modifiers=0; size_t keyCount=0; uint8_t keys[6]={}; };
struct HidConsumerReport { uint16_t usage=0; };
struct HidMouseReport {
  uint8_t buttons=0; int8_t x=0,y=0,wheel=0,pan=0;
  int32_t applyAxisDelta(Axis axis, int32_t delta) {
    int8_t *field = axis==Axis::X ? &x : axis==Axis::Y ? &y : axis==Axis::Wheel ? &wheel : &pan;
    int32_t total = int32_t(*field)+delta;
    if (total>127) { *field=127; return total-127; }
    if (total<-127) { *field=-127; return total+127; }
    *field=int8_t(total); return 0;
  }
};
struct KeySet {};
struct FakeKeyboard {
  bool available=true; unsigned sends=0;
  void configure(const EspBleHidKeyboardConfig &) {}
  template<class F> void onOutputReport(F) {}
  bool ready() const { return available; }
  bool sendReport(const EspBleHidKeyboardReport &) { ++sends; return available; }
  EspBleHidKeyboardOutputReport ledState() const { return {}; }
};
struct FakeConsumer {
  void configure(const EspBleHidConsumerControlConfig &) {}
  bool sendUsage(uint16_t) { return true; }
};
struct FakeMouse {
  bool available=false; unsigned sends=0; EspBleHidMouseReport last{};
  void configure(const EspBleHidMouseConfig &) {}
  bool ready() const { return available; }
  bool sendReport(const EspBleHidMouseReport &report) { ++sends; last=report; return available; }
};
struct EspBle {
  FakeKeyboard keyboard; FakeConsumer consumer; FakeMouse mouse;
  FakeKeyboard &hidKeyboard() { return keyboard; }
  FakeConsumer &hidConsumerControl() { return consumer; }
  FakeMouse &hidMouse() { return mouse; }
};
struct OutputAdapter {
  virtual ~OutputAdapter()=default; virtual void write(const KeySet &)=0;
  virtual bool connected() const=0; virtual bool reportsLockState() const=0;
  virtual bool getLockState(LockState &) const=0;
  virtual void writeAxisDelta(Axis,int32_t)=0;
};
static int32_t nextX=0, nextY=0, nextWheel=0;
HidKeyboardReport buildHidKeyboardReport(const KeySet &) { return {}; }
HidConsumerReport buildHidConsumerReport(const KeySet &) { return {}; }
HidMouseReport buildHidMouseReport(const KeySet &) {
  HidMouseReport report; report.x=int8_t(nextX); report.y=int8_t(nextY); report.wheel=int8_t(nextWheel);
  nextX=nextY=nextWheel=0; return report;
}
'''
        suffix = r'''
}
int main() {
  using namespace esp32keybridge;
  EspBle ble; EspBleHidOutputAdapter output(ble); KeySet keys;
  assert(output.connected());                 // keyboard-ready remains true
  assert(!output.mouseReady());               // mouse report subscription is separate
  nextX=8; nextY=-3; nextWheel=1;
  output.write(keys);                         // failed mouse notification is retained
  assert(ble.mouse.sends==1 && output.mouseSendFailureCount()==1);
  output.discardPendingMouseMotion();          // disarm clears all queued axes, no send
  assert(ble.mouse.sends==1);
  ble.mouse.available=true;
  ble.keyboard.available=false;
  assert(output.mouseReady());
  assert(!output.connected());                 // mouse-ready does not imply keyboard-ready
  ble.keyboard.available=true;
  assert(output.connected());
  output.write(keys);                         // readiness recovery must not replay old motion
  assert(ble.mouse.sends==1);
  nextX=2; output.write(keys);                // new movement is still deliverable
  assert(ble.mouse.sends==2 && ble.mouse.last.x==2 && ble.mouse.last.y==0 && ble.mouse.last.wheel==0);
  assert(output.mouseSendFailureCount()==1);
  return 0;
}
'''
        compiler = host_cpp_compiler()
        source = prelude + self.output_class + suffix
        with tempfile.TemporaryDirectory(prefix="ble-output-safety-") as temp:
            cpp = Path(temp) / "adapter_test.cpp"
            exe = Path(temp) / "adapter_test.exe"
            cpp.write_text(source, encoding="utf-8")
            child_env = os.environ.copy()
            child_env["PATH"] = str(Path(compiler).resolve().parent) + os.pathsep + child_env.get("PATH", "")
            compile_result = subprocess.run([compiler, "-std=c++11", str(cpp), "-o", str(exe)], capture_output=True, text=True, env=child_env)
            self.assertEqual(compile_result.returncode, 0, compile_result.stdout + compile_result.stderr)
            subprocess.run([str(exe)], check=True, capture_output=True, text=True, env=child_env)


if __name__ == "__main__":
    unittest.main()
