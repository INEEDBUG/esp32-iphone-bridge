#pragma once

#include <ESP32KeyBridge.h>
#include <EspBle.h>

#include <string.h>

// BLE adapters backed by the EspBle library: a HID over GATT output
// (peripheral, seen by a PC/phone as a Bluetooth keyboard + consumer + mouse)
// and HID over GATT inputs (central, reading BLE keyboards / mice / gamepads).
//
// Ownership model: the sketch owns the EspBle stack and starts it with the
// library's own configuration (device name, security, advertising). The HID
// service composition is fixed at ble.begin(), so the output adapter declares
// its profiles at construction time — construct it before ble.begin(). The
// input adapters subscribe lazily on their first update(). Neither has a
// begin().
//
// Threading: unlike EspUsbHost, EspBle dispatches user callbacks from the
// context that calls EspBle::update(), not from the stack task, so these
// adapters need no critical section. The sketch must therefore call
// ble.update() from the same context as bridge.update() (the usual loop):
//
//   void loop() { ble.update(); bridge.update(); }
//
// Subscription uses the multi-listener API, so a sketch can keep using every
// on*() hook next to an adapter. The one exception is the keyboard LED
// output report of the output adapter, which is single-slot by design in all
// three libraries — read ble.hidKeyboard().ledState() instead of installing
// onOutputReport() when that adapter is in use.

namespace esp32keybridge
{

// ---------------------------------------------------------------------------
// Output: BLE HID peripheral
// ---------------------------------------------------------------------------

// Composite HID over GATT output: keyboard + consumer + mouse in one HID
// service. The full composition is always declared — a profile that never
// sends a report costs one report descriptor entry and nothing else, which is
// the same trade-off the USB Device adapter makes.
//
// The report formats match EspUsbDeviceHidOutputAdapter (6KRO keyboard,
// one 16-bit consumer usage, 8 buttons + X/Y/Wheel mouse), so a sketch can
// swap or run both outputs with identical behaviour.
class EspBleHidOutputAdapter : public OutputAdapter
{
public:
  // The configs carry HID device identity (manufacturer, VID/PID, layout,
  // boot protocol). Defaults are fine for a plain keyboard; pass your own to
  // control how the host names and treats the device.
  explicit EspBleHidOutputAdapter(
      EspBle &ble,
      const EspBleHidKeyboardConfig &keyboardConfig = EspBleHidKeyboardConfig(),
      const EspBleHidMouseConfig &mouseConfig = EspBleHidMouseConfig(),
      const EspBleHidConsumerControlConfig &consumerConfig = EspBleHidConsumerControlConfig())
      : ble_(ble)
  {
    ble_.hidKeyboard().configure(keyboardConfig);
    ble_.hidConsumerControl().configure(consumerConfig);
    ble_.hidMouse().configure(mouseConfig);

    // The LED value itself is polled from ledState(), which the library keeps
    // updated whether or not a callback is installed. This hook only records
    // that a host has spoken at least once, which ledState() cannot express
    // (no report yet and "all LEDs off" look the same).
    ble_.hidKeyboard().onOutputReport(
        [this](const EspBleHidKeyboardOutputReport &) { ledSeen_ = true; });
  }

  // Packs the key set into keyboard, consumer, and mouse-button reports.
  // Each report is sent when its state changed (or when the previous send
  // failed, so a dropped notification is retried on the next update).
  void write(const KeySet &keys) override
  {
    writeKeyboard(keys);
    writeConsumer(keys);
    writeMouse(keys);
  }

  // True while a host is connected, encrypted (when security is enabled), and
  // subscribed to the keyboard input report. Reports cannot go out before
  // that, so this is what gates the bridge's writes.
  bool connected() const override { return ble_.hidKeyboard().ready(); }

  // Mouse and keyboard input reports have independent HID report IDs and
  // subscriptions. Keep mouse capability explicit instead of inferring it
  // from connected(), which intentionally retains OutputAdapter semantics.
  bool mouseReady() const { return ble_.hidMouse().ready(); }

  // Number of mouse notifications rejected by EspBle since construction.
  // A successful send means the BLE stack accepted the report, not that a
  // remote application acted on it.
  uint32_t mouseSendFailureCount() const { return mouseSendFailureCount_; }

  // Keyboard reports are notifications: a successful local send is not
  // proof that the remote host consumed the report. Keep release retries
  // explicit so callers can fail closed after an uncertain key sequence.
  uint32_t keyboardSendFailureCount() const { return keyboardSendFailureCount_; }

  void requireKeyboardRelease() { keyboardReleasePending_ = true; }
  bool keyboardReleasePending() const { return keyboardReleasePending_; }

  // Send only an all-zero keyboard report. This API never writes consumer or
  // mouse reports and never changes pending axis motion.
  bool retryKeyboardRelease()
  {
    if (!keyboardReleasePending_ || !ble_.hidKeyboard().ready())
    {
      return false;
    }
    const EspBleHidKeyboardReport released = {};
    if (!ble_.hidKeyboard().sendReport(released))
    {
      ++keyboardSendFailureCount_;
      return false;
    }
    lastKeyboardReport_ = released;
    keyboardReleasePending_ = false;
    return true;
  }

  // Forget every queued relative axis without producing a HID report. Call
  // this when the control session is disarmed so a later-ready peer cannot
  // receive stale movement.
  void discardPendingMouseMotion()
  {
    for (size_t axis = 0; axis < kAxisCount; ++axis)
    {
      pendingAxis_[axis] = 0;
    }
  }

  // LED output reports from the host provide the lock state. False until the
  // first report arrives; the bridge keeps its shadow meanwhile.
  bool reportsLockState() const override { return true; }
  bool getLockState(LockState &out) const override
  {
    if (!ledSeen_)
    {
      return false;
    }
    const EspBleHidKeyboardOutputReport report = ble_.hidKeyboard().ledState();
    out.numLock = report.numLock;
    out.capsLock = report.capsLock;
    out.scrollLock = report.scrollLock;
    out.kana = report.kana;
    return true;
  }

  // Relative axis totals accumulate here; the bridge delivers them before
  // write() in the same update, so they ride the mouse report of the same
  // cycle (int8 saturation with the remainder carried to the next update).
  // The HID mouse report has no pan field, so Pan is dropped.
  void writeAxisDelta(Axis axis, int32_t delta) override
  {
    if (axis == Axis::Pan)
    {
      return;
    }
    pendingAxis_[static_cast<size_t>(axis)] += delta;
  }

private:
  void writeKeyboard(const KeySet &keys)
  {
    EspBleHidKeyboardReport bleReport = {};
    if (!keyboardReleasePending_)
    {
      const HidKeyboardReport report = buildHidKeyboardReport(keys);
      bleReport.modifiers = report.modifiers;
      for (size_t i = 0; i < HidKeyboardReport::MaxKeys; ++i)
      {
        bleReport.keys[i] = i < report.keyCount ? report.keys[i] : 0;
      }
    }

    if (!keyboardReleasePending_ &&
        memcmp(&bleReport, &lastKeyboardReport_, sizeof(bleReport)) == 0)
    {
      return;
    }
    if (ble_.hidKeyboard().sendReport(bleReport))
    {
      lastKeyboardReport_ = bleReport;
      keyboardReleasePending_ = false;
    }
    else
    {
      ++keyboardSendFailureCount_;
      const EspBleHidKeyboardReport released = {};
      if (memcmp(&bleReport, &released, sizeof(bleReport)) == 0)
      {
        keyboardReleasePending_ = true;
      }
    }
  }

  void writeConsumer(const KeySet &keys)
  {
    const HidConsumerReport report = buildHidConsumerReport(keys);
    if (!consumerPending_ && report.usage == lastConsumerUsage_)
    {
      return;
    }
    lastConsumerUsage_ = report.usage;
    consumerPending_ = !ble_.hidConsumerControl().sendUsage(report.usage);
  }

  void writeMouse(const KeySet &keys)
  {
    HidMouseReport report = buildHidMouseReport(keys);
    for (size_t axis = 0; axis < kAxisCount; ++axis)
    {
      pendingAxis_[axis] = report.applyAxisDelta(static_cast<Axis>(axis), pendingAxis_[axis]);
    }
    const bool moved = report.x != 0 || report.y != 0 || report.wheel != 0;
    // lastMouseButtons_ tracks the last successful send, so a failed button
    // change keeps mismatching and retries on the next update.
    if (!moved && report.buttons == lastMouseButtons_)
    {
      return;
    }

    EspBleHidMouseReport bleReport;
    bleReport.buttons = report.buttons;
    bleReport.x = report.x;
    bleReport.y = report.y;
    bleReport.wheel = report.wheel;
    if (ble_.hidMouse().sendReport(bleReport))
    {
      lastMouseButtons_ = report.buttons;
    }
    else
    {
      // Not sent: put the movement back so nothing is lost.
      ++mouseSendFailureCount_;
      pendingAxis_[static_cast<size_t>(Axis::X)] += report.x;
      pendingAxis_[static_cast<size_t>(Axis::Y)] += report.y;
      pendingAxis_[static_cast<size_t>(Axis::Wheel)] += report.wheel;
    }
  }

  EspBle &ble_;
  EspBleHidKeyboardReport lastKeyboardReport_;
  bool keyboardReleasePending_ = false;
  uint32_t keyboardSendFailureCount_ = 0;
  uint16_t lastConsumerUsage_ = 0;
  bool consumerPending_ = false;
  uint8_t lastMouseButtons_ = 0;
  int32_t pendingAxis_[kAxisCount] = {};
  uint32_t mouseSendFailureCount_ = 0;
  bool ledSeen_ = false;
};

// ---------------------------------------------------------------------------
// Input: BLE HID host (central)
// ---------------------------------------------------------------------------
//
// The sketch drives connection setup itself (scan -> connect -> pair ->
// hidHost().discover()), exactly as in EspBle's own KeyboardHost example;
// these adapters only consume the resulting HID reports. Nothing here needs a
// connection to exist yet, so they can be registered with the bridge up front.

// BLE keyboard input (consumer keys included). All connected HID keyboards
// are merged (key union across peers); a disconnect releases only that peer's
// keys. The bridge lock state is forwarded to every keyboard's LEDs.
//
// Keyboard state comes from onKeyboardState: one format-independent 256-bit
// usage snapshot per changed report — boot, report-protocol and NKRO alike,
// modifier-only changes included — which maps directly onto the key set, the
// same way EspUsbHostKeyboardInputAdapter reads its stack.
class EspBleHidHostKeyboardInputAdapter : public InputAdapter
{
public:
  // Keyboard connections tracked simultaneously; reports from further ones
  // are ignored.
  static constexpr size_t MaxKeyboards = 4;
  // Connections holding a consumer usage. One entry per connection, not per
  // usage: the HOGP consumer report carries a single usage at a time.
  static constexpr size_t MaxConsumerSources = 4;

  explicit EspBleHidHostKeyboardInputAdapter(EspBle &ble) : ble_(ble) {}

  ~EspBleHidHostKeyboardInputAdapter() override
  {
    ble_.hidHost().removeListener(stateListener_);
    ble_.hidHost().removeListener(consumerListener_);
    ble_.removeConnectionListener(disconnectListener_);
  }

  void update() override
  {
    if (!subscribed_)
    {
      subscribed_ = true;
      stateListener_ = ble_.hidHost().addKeyboardStateListener(
          [this](const EspBleHidKeyboardState &state) { handleKeyboardState(state); });
      consumerListener_ = ble_.hidHost().addConsumerControlListener(
          [this](const EspBleHidConsumerControlEvent &event) { handleConsumer(event); });
      disconnectListener_ = ble_.addDisconnectedListener(
          [this](const EspBleConnection &connection) { handleDisconnect(connection.id); });
    }

    keys_.clear();
    for (size_t i = 0; i < keyboardCount_; ++i)
    {
      for (size_t byte = 0; byte < EspBleHidKeyboardState::BitmapSize; ++byte)
      {
        uint8_t bits = keyboards_[i].bitmap[byte];
        while (bits != 0)
        {
          const int bit = __builtin_ctz(bits);
          bits = static_cast<uint8_t>(bits & (bits - 1));
          const uint16_t usage = static_cast<uint16_t>(byte * 8 + bit);
          // Usages 0x01-0x03 are boot rollover/POST error codes, not keys.
          if (usage >= 0x04)
          {
            keys_.press(keyboardKey(usage));
          }
        }
      }
    }
    for (size_t i = 0; i < consumerCount_; ++i)
    {
      keys_.press(consumerKey(consumer_[i].usage));
    }
    connected_ = keyboardCount_ > 0 || consumerCount_ > 0;
  }

  const KeySet &keys() const override { return keys_; }

  // A keyboard counts as present from its first report; false again when
  // every known peer disconnected. A keyboard that never reported has no
  // state to lose.
  bool connected() const override { return connected_; }

  // Forwards the bridge lock state to the LEDs of every tracked keyboard.
  // EspBle carries kana too, so unlike the USB Host side nothing is dropped.
  void setLockState(const LockState &state) override
  {
    for (size_t i = 0; i < keyboardCount_; ++i)
    {
      ble_.hidHost().setKeyboardLeds(
          keyboards_[i].connectionId, state.numLock, state.capsLock, state.scrollLock, false,
          state.kana);
    }
  }

private:
  struct Keyboard
  {
    EspBleConnectionId connectionId = 0;
    uint8_t reportId = 0;
    uint8_t bitmap[EspBleHidKeyboardState::BitmapSize] = {};
  };

  struct ConsumerEntry
  {
    EspBleConnectionId connectionId = 0;
    uint16_t usage = 0;
  };

  void handleKeyboardState(const EspBleHidKeyboardState &state)
  {
    size_t index = keyboardCount_;
    for (size_t i = 0; i < keyboardCount_; ++i)
    {
      if (keyboards_[i].connectionId == state.connectionId &&
          keyboards_[i].reportId == state.reportId)
      {
        index = i;
        break;
      }
    }
    if (index == keyboardCount_ && keyboardCount_ < MaxKeyboards)
    {
      keyboards_[keyboardCount_].connectionId = state.connectionId;
      keyboards_[keyboardCount_].reportId = state.reportId;
      ++keyboardCount_;
    }
    if (index < keyboardCount_)
    {
      memcpy(keyboards_[index].bitmap, state.bitmap, EspBleHidKeyboardState::BitmapSize);
    }
  }

  // The HOGP consumer report carries one 16-bit usage at a time, so the event
  // reports the connection's *new* usage and 0 means "nothing held". A release
  // therefore arrives as usage 0 rather than as the usage that was let go —
  // unlike EspUsbHost, whose consumer event names the released usage. So this
  // replaces the connection's entry instead of looking for a matching usage
  // to remove; matching on the usage would never find one and the key would
  // stay stuck.
  void handleConsumer(const EspBleHidConsumerControlEvent &event)
  {
    size_t index = consumerCount_;
    for (size_t i = 0; i < consumerCount_; ++i)
    {
      if (consumer_[i].connectionId == event.connectionId)
      {
        index = i;
        break;
      }
    }

    if (event.usage == 0)
    {
      if (index < consumerCount_)
      {
        consumer_[index] = consumer_[consumerCount_ - 1];
        --consumerCount_;
      }
      return;
    }

    if (index == consumerCount_)
    {
      if (consumerCount_ >= MaxConsumerSources)
      {
        return;
      }
      index = consumerCount_++;
      consumer_[index].connectionId = event.connectionId;
    }
    consumer_[index].usage = event.usage;
  }

  // Any connection, not only keyboards.
  void handleDisconnect(EspBleConnectionId connectionId)
  {
    for (size_t i = keyboardCount_; i > 0; --i)
    {
      if (keyboards_[i - 1].connectionId == connectionId)
      {
        keyboards_[i - 1] = keyboards_[keyboardCount_ - 1];
        --keyboardCount_;
      }
    }
    for (size_t i = consumerCount_; i > 0; --i)
    {
      if (consumer_[i - 1].connectionId == connectionId)
      {
        consumer_[i - 1] = consumer_[consumerCount_ - 1];
        --consumerCount_;
      }
    }
  }

  EspBle &ble_;
  bool subscribed_ = false;
  EspBleListenerId stateListener_ = EspBleInvalidListenerId;
  EspBleListenerId consumerListener_ = EspBleInvalidListenerId;
  EspBleListenerId disconnectListener_ = EspBleInvalidListenerId;
  KeySet keys_;
  bool connected_ = false;
  Keyboard keyboards_[MaxKeyboards];
  size_t keyboardCount_ = 0;
  ConsumerEntry consumer_[MaxConsumerSources];
  size_t consumerCount_ = 0;
};

// BLE mouse input. Buttons become MouseButton keys in keys(); movement and
// wheel accumulate as relative axis deltas. All connected mice are merged
// (buttons union, deltas summed); a disconnect releases that mouse's buttons
// only. HID over GATT mouse reports carry no AC Pan field.
class EspBleHidHostMouseInputAdapter : public InputAdapter
{
public:
  static constexpr size_t MaxMice = 4;

  explicit EspBleHidHostMouseInputAdapter(EspBle &ble) : ble_(ble) {}

  ~EspBleHidHostMouseInputAdapter() override
  {
    ble_.hidHost().removeListener(mouseListener_);
    ble_.removeConnectionListener(disconnectListener_);
  }

  void update() override
  {
    if (!subscribed_)
    {
      subscribed_ = true;
      mouseListener_ = ble_.hidHost().addMouseListener(
          [this](const EspBleHidMouseEvent &event) { handleMouse(event); });
      disconnectListener_ = ble_.addDisconnectedListener(
          [this](const EspBleConnection &connection) { handleDisconnect(connection.id); });
    }

    keys_.clear();
    for (size_t i = 0; i < mouseCount_; ++i)
    {
      for (size_t bit = 0; bit < 8; ++bit)
      {
        if ((mice_[i].buttons & (1u << bit)) != 0)
        {
          keys_.press(mouseButtonKey(static_cast<uint16_t>(bit + 1)));
        }
      }
    }
    connected_ = mouseCount_ > 0;
  }

  const KeySet &keys() const override { return keys_; }
  bool connected() const override { return connected_; }

  int32_t takeAxisDelta(Axis axis) override
  {
    const size_t index = static_cast<size_t>(axis);
    const int32_t delta = axisDeltas_[index];
    axisDeltas_[index] = 0;
    return delta;
  }

private:
  struct Mouse
  {
    EspBleConnectionId connectionId = 0;
    uint8_t buttons = 0;
  };

  void handleMouse(const EspBleHidMouseEvent &event)
  {
    size_t index = mouseCount_;
    for (size_t i = 0; i < mouseCount_; ++i)
    {
      if (mice_[i].connectionId == event.connectionId)
      {
        index = i;
        break;
      }
    }
    if (index == mouseCount_ && mouseCount_ < MaxMice)
    {
      mice_[mouseCount_].connectionId = event.connectionId;
      mice_[mouseCount_].buttons = 0;
      ++mouseCount_;
    }
    if (index < mouseCount_)
    {
      mice_[index].buttons = event.buttons;
      axisDeltas_[static_cast<size_t>(Axis::X)] += event.x;
      axisDeltas_[static_cast<size_t>(Axis::Y)] += event.y;
      axisDeltas_[static_cast<size_t>(Axis::Wheel)] += event.wheel;
    }
  }

  // Any connection, not only mice.
  void handleDisconnect(EspBleConnectionId connectionId)
  {
    for (size_t i = 0; i < mouseCount_; ++i)
    {
      if (mice_[i].connectionId == connectionId)
      {
        mice_[i] = mice_[mouseCount_ - 1];
        --mouseCount_;
        break;
      }
    }
  }

  EspBle &ble_;
  bool subscribed_ = false;
  EspBleListenerId mouseListener_ = EspBleInvalidListenerId;
  EspBleListenerId disconnectListener_ = EspBleInvalidListenerId;
  KeySet keys_;
  bool connected_ = false;
  int32_t axisDeltas_[kAxisCount] = {};
  Mouse mice_[MaxMice];
  size_t mouseCount_ = 0;
};

// BLE gamepad input. Buttons and the hat/D-pad map to keys through the core's
// GamepadKeyMap, so mapping and hat decoding are identical to
// EspUsbHostGamepadInputAdapter. Only mapped controls produce output. All
// connected pads are merged; a disconnect drops only that pad's controls.
// Analog sticks and triggers are not mapped in this version.
class EspBleHidHostGamepadInputAdapter : public InputAdapter
{
public:
  static constexpr size_t MaxGamepads = 4;
  static constexpr size_t MaxButtons = GamepadKeyMap::MaxButtons;

  explicit EspBleHidHostGamepadInputAdapter(EspBle &ble) : ble_(ble) {}

  ~EspBleHidHostGamepadInputAdapter() override
  {
    ble_.hidHost().removeListener(gamepadListener_);
    ble_.removeConnectionListener(disconnectListener_);
  }

  bool mapButton(uint8_t number, Key key) { return map_.mapButton(number, key); }
  void mapHat(Key up, Key down, Key left, Key right) { map_.mapHat(up, down, left, right); }

  void update() override
  {
    if (!subscribed_)
    {
      subscribed_ = true;
      gamepadListener_ = ble_.hidHost().addGamepadListener(
          [this](const EspBleHidGamepadEvent &event) { handleGamepad(event); });
      disconnectListener_ = ble_.addDisconnectedListener(
          [this](const EspBleConnection &connection) { handleDisconnect(connection.id); });
    }

    keys_.clear();
    for (size_t i = 0; i < padCount_; ++i)
    {
      map_.addKeys(keys_, pads_[i].buttons, pads_[i].hat);
    }
    connected_ = padCount_ > 0;
  }

  const KeySet &keys() const override { return keys_; }
  bool connected() const override { return connected_; }

private:
  struct Pad
  {
    EspBleConnectionId connectionId = 0;
    uint8_t reportId = 0;
    uint32_t buttons = 0;
    int8_t hat = GamepadKeyMap::HatCentered;
  };

  void handleGamepad(const EspBleHidGamepadEvent &event)
  {
    uint32_t buttons = 0;
    int8_t hat = GamepadKeyMap::HatCentered;
    GamepadKeyMap::decodeFields(event.fields, event.fieldCount, buttons, hat);

    size_t index = padCount_;
    for (size_t i = 0; i < padCount_; ++i)
    {
      if (pads_[i].connectionId == event.connectionId && pads_[i].reportId == event.reportId)
      {
        index = i;
        break;
      }
    }
    if (index == padCount_ && padCount_ < MaxGamepads)
    {
      pads_[padCount_].connectionId = event.connectionId;
      pads_[padCount_].reportId = event.reportId;
      ++padCount_;
    }
    if (index < padCount_)
    {
      pads_[index].buttons = buttons;
      pads_[index].hat = hat;
    }
  }

  // Any connection, not only gamepads.
  void handleDisconnect(EspBleConnectionId connectionId)
  {
    for (size_t i = padCount_; i > 0; --i)
    {
      if (pads_[i - 1].connectionId == connectionId)
      {
        pads_[i - 1] = pads_[padCount_ - 1];
        --padCount_;
      }
    }
  }

  EspBle &ble_;
  bool subscribed_ = false;
  EspBleListenerId gamepadListener_ = EspBleInvalidListenerId;
  EspBleListenerId disconnectListener_ = EspBleInvalidListenerId;
  KeySet keys_;
  bool connected_ = false;
  GamepadKeyMap map_;
  Pad pads_[MaxGamepads];
  size_t padCount_ = 0;
};

} // namespace esp32keybridge
