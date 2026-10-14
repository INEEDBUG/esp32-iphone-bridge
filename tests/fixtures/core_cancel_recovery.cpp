#include <cassert>
#include <cstdint>
#include <cstring>
#include <vector>

#include "ESP32KeyBridge.h"

struct EspBleHidKeyboardConfig {};
struct EspBleHidMouseConfig {};
struct EspBleHidConsumerControlConfig {};
struct EspBleHidKeyboardOutputReport
{
  bool numLock = false, capsLock = false, scrollLock = false, kana = false;
};
struct EspBleHidKeyboardReport
{
  uint8_t modifiers = 0, keys[6] = {};
};
struct EspBleHidMouseReport
{
  uint8_t buttons = 0;
  int8_t x = 0, y = 0, wheel = 0;
};

struct FakeKeyboard
{
  bool available = true;
  void configure(const EspBleHidKeyboardConfig &) {}
  template<class F> void onOutputReport(F) {}
  bool ready() const { return available; }
  bool sendReport(const EspBleHidKeyboardReport &) { return available; }
  EspBleHidKeyboardOutputReport ledState() const { return {}; }
};
struct FakeConsumer
{
  void configure(const EspBleHidConsumerControlConfig &) {}
  bool sendUsage(uint16_t) { return true; }
};
struct FakeMouse
{
  bool available = true;
  std::vector<EspBleHidMouseReport> reports;
  void configure(const EspBleHidMouseConfig &) {}
  bool ready() const { return available; }
  bool sendReport(const EspBleHidMouseReport &report)
  {
    reports.push_back(report);
    return available;
  }
};
struct EspBle
{
  FakeKeyboard keyboard;
  FakeConsumer consumer;
  FakeMouse mouse;
  FakeKeyboard &hidKeyboard() { return keyboard; }
  FakeConsumer &hidConsumerControl() { return consumer; }
  FakeMouse &hidMouse() { return mouse; }
};

namespace esp32keybridge
{
/*__PRODUCTION_OUTPUT_ADAPTER__*/
}

static void releaseAllEquivalent(esp32keybridge::ESP32KeyBridge &bridge,
                                 esp32keybridge::ManualInputAdapter &input,
                                 esp32keybridge::EspBleHidOutputAdapter &output,
                                 EspBle &ble)
{
  using namespace esp32keybridge;
  bridge.cancelText();
  input.clear();
  for (size_t axis = 0; axis < kAxisCount; ++axis)
  {
    input.takeAxisDelta(static_cast<Axis>(axis));
  }
  output.discardPendingMouseMotion();
  output.requireKeyboardRelease();
  if (output.connected()) output.retryKeyboardRelease();
  const EspBleHidMouseReport releasedMouse = {};
  if (output.mouseReady()) ble.hidMouse().sendReport(releasedMouse);
  output.discardPendingMouseMotion();
}

int main()
{
  using namespace esp32keybridge;
  const Key mouse1 = mouseButtonKey(1);

  // Without an unheld core update, direct STOP release leaves stale core and
  // adapter button state; the next same-button press is therefore suppressed.
  {
    EspBle ble;
    ESP32KeyBridge bridge;
    ManualInputAdapter input;
    EspBleHidOutputAdapter output(ble);
    assert(bridge.addInput(input) && bridge.addOutput(output));
    assert(input.press(mouse1));
    bridge.update();
    assert(ble.mouse.reports.size() == 1 && ble.mouse.reports[0].buttons == 1);
    releaseAllEquivalent(bridge, input, output, ble);
    assert(bridge.outputKeys().contains(mouse1));
    const size_t beforeNewPress = ble.mouse.reports.size();
    assert(input.press(mouse1));
    bridge.update();
    assert(ble.mouse.reports.size() == beforeNewPress);
  }

  // A complete release that passes through bridge.update synchronizes the
  // core and adapter; the following press is emitted after STOP.
  {
    EspBle ble;
    ESP32KeyBridge bridge;
    ManualInputAdapter input;
    EspBleHidOutputAdapter output(ble);
    assert(bridge.addInput(input) && bridge.addOutput(output));
    assert(input.press(mouse1));
    input.addAxisDelta(Axis::X, 6);
    bridge.update();
    assert(ble.mouse.reports.size() == 1 && ble.mouse.reports[0].buttons == 1
           && ble.mouse.reports[0].x == 6);
    assert(input.release(mouse1));
    bridge.update();
    assert(ble.mouse.reports.size() == 2 && ble.mouse.reports[1].buttons == 0);
    releaseAllEquivalent(bridge, input, output, ble);
    assert(ble.mouse.reports.size() == 3 && ble.mouse.reports[2].buttons == 0);
    assert(input.press(mouse1));
    bridge.update();
    assert(ble.mouse.reports.size() == 4 && ble.mouse.reports[3].buttons == 1
           && ble.mouse.reports[3].x == 0 && ble.mouse.reports[3].y == 0
           && ble.mouse.reports[3].wheel == 0);
  }

  // An unheld Y=1 update after STOP clears the stale held/cache state. Axes
  // queued before cancellation must not leak into that nudge or the press.
  {
    EspBle ble;
    ESP32KeyBridge bridge;
    ManualInputAdapter input;
    EspBleHidOutputAdapter output(ble);
    assert(bridge.addInput(input) && bridge.addOutput(output));
    assert(input.press(mouse1));
    bridge.update();
    assert(ble.mouse.reports.size() == 1 && ble.mouse.reports[0].buttons == 1);
    input.addAxisDelta(Axis::X, 9);
    releaseAllEquivalent(bridge, input, output, ble);
    assert(bridge.outputKeys().contains(mouse1));
    input.addAxisDelta(Axis::Y, 1);
    bridge.update();
    assert(ble.mouse.reports.size() == 3);
    assert(ble.mouse.reports[2].buttons == 0 && ble.mouse.reports[2].x == 0
           && ble.mouse.reports[2].y == 1 && ble.mouse.reports[2].wheel == 0);
    assert(input.press(mouse1));
    bridge.update();
    assert(ble.mouse.reports.size() == 4 && ble.mouse.reports[3].buttons == 1
           && ble.mouse.reports[3].x == 0 && ble.mouse.reports[3].y == 0
           && ble.mouse.reports[3].wheel == 0);
  }
  return 0;
}
