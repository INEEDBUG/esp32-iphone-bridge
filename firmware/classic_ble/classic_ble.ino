#include <ArduinoJson.h>
#include <ESP32KeyBridge.h>
#include <ESP32KeyBridgeEspBle.h>
#include <EspBle.h>

#include <stdint.h>
#include <string.h>

namespace
{
constexpr size_t MAX_FRAME_BYTES = 512;
constexpr size_t MAX_TOKEN_BYTES = 64;
constexpr size_t MAX_TYPE_BYTES = 128;
constexpr int32_t MAX_MOVE_DELTA = 2048;
constexpr int32_t MAX_SCROLL_DELTA = 127;
constexpr uint32_t MIN_TTL_MS = 1;
constexpr uint32_t MAX_TTL_MS = 5000;
constexpr uint32_t SESSION_WATCHDOG_MS = 1500;
constexpr uint32_t MOVE_STEP_MS = 5;
constexpr int32_t MOVE_STEP = 10;
constexpr int HOME_REPORTS = 50;

EspBle ble;
esp32keybridge::ESP32KeyBridge bridge;
esp32keybridge::ManualInputAdapter commandInput;
esp32keybridge::EspBleHidOutputAdapter hidOutput(ble);

String activeSession;
uint32_t lastHostActivity = 0;
bool cancelRequested = false;
const char *cancelReason = "cancelled";
bool lastBleReady = false;
bool lastMouseReady = false;
uint32_t mouseFailureAtActionStart = 0;
bool bleStarted = false;
String seenIds[32];
size_t seenIdNext = 0;
size_t seenIdCount = 0;

char rxBuffer[MAX_FRAME_BYTES + 1] = {};
size_t rxLength = 0;
bool rxOverflow = false;

struct Request
{
  String id;
  String session;
  String op;
  String text;
  int32_t dx = 0;
  int32_t dy = 0;
  int32_t vertical = 0;
  int32_t horizontal = 0;
  uint32_t ttlMs = 0;
  uint32_t receivedAt = 0;
};

bool tokenValid(const String &value)
{
  if (value.isEmpty() || value.length() > MAX_TOKEN_BYTES)
  {
    return false;
  }
  for (size_t i = 0; i < value.length(); ++i)
  {
    const char c = value[i];
    const bool asciiLetterOrDigit = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9');
    if (!(asciiLetterOrDigit || c == '-' || c == '_' || c == '.' || c == ':' || c == '~'))
    {
      return false;
    }
  }
  return true;
}

bool readInteger(JsonVariantConst value, int32_t &out)
{
  if (value.is<bool>() || !value.is<int32_t>())
  {
    return false;
  }
  out = value.as<int32_t>();
  return true;
}

bool readArgInt(JsonObjectConst args, const char *name, int32_t &out)
{
  return args.containsKey(name) && readInteger(args[name], out);
}

bool copyJsonString(JsonVariantConst value, String &out)
{
  if (!value.is<JsonString>()) return false;
  const JsonString jsonString = value.as<JsonString>();
  const char *cString = jsonString.c_str();
  if (strlen(cString) != jsonString.size()) return false;
  out = cString;
  return true;
}

bool knownOperation(const String &op)
{
  return op == "hello" || op == "move" || op == "click" || op == "press" || op == "release" ||
         op == "scroll" || op == "type" || op == "key" || op == "stop" || op == "ping" || op == "home";
}

bool parseRequest(const char *line, Request &request, const char *&error)
{
  JsonDocument doc;
  const DeserializationError parseError = deserializeJson(doc, line, DeserializationOption::NestingLimit(4));
  if (parseError || !doc.is<JsonObjectConst>())
  {
    error = "invalid_json";
    return false;
  }

  JsonObjectConst object = doc.as<JsonObjectConst>();
  if (!object["args"].is<JsonObjectConst>() ||
      object["ttl_ms"].is<bool>() || !object["ttl_ms"].is<uint32_t>())
  {
    error = "invalid_schema";
    return false;
  }

  if (!copyJsonString(object["id"], request.id) || !copyJsonString(object["session"], request.session) ||
      !copyJsonString(object["op"], request.op))
  {
    error = "invalid_schema";
    return false;
  }
  request.ttlMs = object["ttl_ms"].as<uint32_t>();
  request.receivedAt = millis();
  JsonObjectConst args = object["args"].as<JsonObjectConst>();

  if (!tokenValid(request.id) || !tokenValid(request.session))
  {
    error = "invalid_token";
    return false;
  }
  if (!knownOperation(request.op))
  {
    error = "unknown_operation";
    return false;
  }
  if (request.ttlMs < MIN_TTL_MS || request.ttlMs > MAX_TTL_MS)
  {
    error = "invalid_ttl";
    return false;
  }

  bool exactArgs = false;
  if (request.op == "move")
  {
    exactArgs = args.size() == 2 && args.containsKey("dx") && args.containsKey("dy");
  }
  else if (request.op == "scroll")
  {
    exactArgs = args.size() == 2 && args.containsKey("v") && args.containsKey("h");
  }
  else if (request.op == "type")
  {
    exactArgs = args.size() == 1 && args.containsKey("text");
  }
  else if (request.op == "key")
  {
    exactArgs = args.size() == 1 && args.containsKey("key");
  }
  else
  {
    exactArgs = args.size() == 0;
  }
  if (!exactArgs)
  {
    error = "invalid_args";
    return false;
  }

  if (request.op == "move")
  {
    if (!readArgInt(args, "dx", request.dx) || !readArgInt(args, "dy", request.dy) ||
        request.dx < -MAX_MOVE_DELTA || request.dx > MAX_MOVE_DELTA ||
        request.dy < -MAX_MOVE_DELTA || request.dy > MAX_MOVE_DELTA)
    {
      error = "invalid_move";
      return false;
    }
  }
  else if (request.op == "scroll")
  {
    if (!readArgInt(args, "v", request.vertical) || !readArgInt(args, "h", request.horizontal) ||
        request.vertical < -MAX_SCROLL_DELTA || request.vertical > MAX_SCROLL_DELTA ||
        request.horizontal < -MAX_SCROLL_DELTA || request.horizontal > MAX_SCROLL_DELTA)
    {
      error = "invalid_scroll";
      return false;
    }
    if (request.horizontal != 0)
    {
      error = "horizontal_scroll_unsupported";
      return false;
    }
  }
  else if (request.op == "type")
  {
    if (!copyJsonString(args["text"], request.text))
    {
      error = "invalid_text";
      return false;
    }
    if (request.text.length() > MAX_TYPE_BYTES)
    {
      error = "text_too_long";
      return false;
    }
    for (size_t i = 0; i < request.text.length(); ++i)
    {
      const unsigned char c = request.text[i];
      if (c < 0x20 || c > 0x7e)
      {
        error = "text_not_printable_ascii";
        return false;
      }
    }
  }
  else if (request.op == "key")
  {
    if (!copyJsonString(args["key"], request.text))
    {
      error = "invalid_key";
      return false;
    }
    if (!(request.text == "enter" || request.text == "backspace" || request.text == "escape" || request.text == "tab"))
    {
      error = "key_not_allowed";
      return false;
    }
  }
  return true;
}

void sendReply(const String &id, const String &session, const char *state, const char *error = nullptr)
{
  JsonDocument response;
  response["id"] = id;
  response["session"] = session;
  response["state"] = state;
  if (error != nullptr)
  {
    response["error"] = error;
  }
  response["ble_connected"] = hidOutput.connected();
  response["mouse_ready"] = hidOutput.mouseReady();
  response["mouse_send_failures"] = hidOutput.mouseSendFailureCount();
  serializeJson(response, Serial);
  Serial.write('\n');
}

void sendMalformed(const char *error)
{
  sendReply("", "", "rejected", error);
}

bool expired(const Request &request)
{
  return static_cast<int32_t>(millis() - (request.receivedAt + request.ttlMs)) >= 0;
}

void releaseAll(bool transmitRelease)
{
  bridge.cancelText();
  commandInput.clear();
  for (size_t axis = 0; axis < esp32keybridge::kAxisCount; ++axis)
  {
    commandInput.takeAxisDelta(static_cast<esp32keybridge::Axis>(axis));
  }
  hidOutput.discardPendingMouseMotion();
  if (transmitRelease && hidOutput.connected())
  {
    bridge.update();
  }
  // Mouse subscription can survive loss of keyboard readiness. Release its
  // buttons independently with zero axes; never infer receipt on the phone.
  if (transmitRelease && hidOutput.mouseReady())
  {
    const EspBleHidMouseReport releasedMouse = {};
    ble.hidMouse().sendReport(releasedMouse);
  }
  hidOutput.discardPendingMouseMotion();
}

bool requestIdSeen(const String &id)
{
  for (size_t i = 0; i < seenIdCount; ++i)
  {
    if (seenIds[i] == id) return true;
  }
  return false;
}

void rememberRequestId(const String &id)
{
  seenIds[seenIdNext] = id;
  seenIdNext = (seenIdNext + 1) % 32;
  if (seenIdCount < 32) ++seenIdCount;
}

void resetRequestIds()
{
  for (size_t i = 0; i < 32; ++i) seenIds[i] = "";
  seenIdNext = 0;
  seenIdCount = 0;
}

void disarm(const char *reason, bool transmitRelease)
{
  releaseAll(transmitRelease);
  activeSession = "";
  lastHostActivity = 0;
  cancelRequested = true;
  cancelReason = reason;
}

void armSession(const Request &request)
{
  releaseAll(true);
  cancelRequested = false;
  cancelReason = "cancelled";
  activeSession = request.session;
  lastHostActivity = millis();
}

int readLine(String &line)
{
  while (Serial.available() > 0)
  {
    const int incoming = Serial.read();
    if (incoming < 0)
    {
      break;
    }
    const char c = static_cast<char>(incoming);
    if (c == '\n')
    {
      if (rxOverflow)
      {
        rxOverflow = false;
        rxLength = 0;
        sendMalformed("frame_too_large");
        return -1;
      }
      if (rxLength > 0 && rxBuffer[rxLength - 1] == '\r')
      {
        --rxLength;
      }
      rxBuffer[rxLength] = '\0';
      line = rxBuffer;
      rxLength = 0;
      return 1;
    }
    if (rxOverflow)
    {
      continue;
    }
    if (rxLength >= MAX_FRAME_BYTES)
    {
      rxOverflow = true;
      continue;
    }
    rxBuffer[rxLength++] = c;
  }
  return 0;
}

bool matchingSession(const Request &request)
{
  return !activeSession.isEmpty() && request.session == activeSession;
}

void handleInterrupt(Request &request)
{
  if (expired(request))
  {
    sendReply(request.id, request.session, "rejected", "request_expired");
    return;
  }
  if (request.op == "hello")
  {
    if (request.session == activeSession)
    {
      sendReply(request.id, request.session, "rejected", "session_not_new");
      return;
    }
    resetRequestIds();
    rememberRequestId(request.id);
    armSession(request);
    cancelRequested = true;
    cancelReason = "session_replaced";
    sendReply(request.id, request.session, "executed");
    return;
  }
  if (!matchingSession(request))
  {
    sendReply(request.id, request.session, "rejected", "session_mismatch");
    return;
  }
  if (requestIdSeen(request.id))
  {
    sendReply(request.id, request.session, "rejected", "duplicate_id");
    return;
  }
  rememberRequestId(request.id);
  lastHostActivity = millis();
  if (request.op == "ping")
  {
    sendReply(request.id, request.session, "executed");
  }
  else if (request.op == "stop")
  {
    disarm("stopped", true);
    sendReply(request.id, request.session, "executed");
  }
  else
  {
    sendReply(request.id, request.session, "rejected", "busy");
  }
}

void pollWhileBusy(const Request &current)
{
  String line;
  const int lineResult = readLine(line);
  if (lineResult < 0)
  {
    return;
  }
  if (lineResult == 0)
  {
    return;
  }
  Request interruptRequest;
  const char *error = nullptr;
  if (!parseRequest(line.c_str(), interruptRequest, error))
  {
    sendMalformed(error);
    return;
  }
  handleInterrupt(interruptRequest);
  (void)current;
}

bool actionCanContinue(const Request &request)
{
  ble.update();
  if (lastBleReady && !hidOutput.connected())
  {
    disarm("ble_disconnected", true);
  }
  lastBleReady = hidOutput.connected();
  const bool mouseOperation = request.op == "move" || request.op == "home" ||
      request.op == "click" || request.op == "press" || request.op == "release" || request.op == "scroll";
  if (mouseOperation && !hidOutput.mouseReady())
  {
    disarm("mouse_not_ready", true);
  }
  if (mouseOperation && hidOutput.mouseSendFailureCount() != mouseFailureAtActionStart)
  {
    disarm("mouse_report_failed", true);
  }
  if (!activeSession.isEmpty() && millis() - lastHostActivity > SESSION_WATCHDOG_MS)
  {
    disarm("session_timeout", true);
  }
  pollWhileBusy(request);
  if (cancelRequested)
  {
    return false;
  }
  if (expired(request))
  {
    disarm("request_expired", true);
    return false;
  }
  return hidOutput.connected();
}

bool movePaced(const Request &request, int32_t dx, int32_t dy)
{
  const int32_t stepsX = (abs(dx) + MOVE_STEP - 1) / MOVE_STEP;
  const int32_t stepsY = (abs(dy) + MOVE_STEP - 1) / MOVE_STEP;
  const int32_t count = max(stepsX, stepsY);
  int32_t remainingX = dx;
  int32_t remainingY = dy;
  for (int32_t i = 0; i < count; ++i)
  {
    if (!actionCanContinue(request))
    {
      return false;
    }
    const int32_t sx = constrain(remainingX, -MOVE_STEP, MOVE_STEP);
    const int32_t sy = constrain(remainingY, -MOVE_STEP, MOVE_STEP);
    commandInput.addAxisDelta(esp32keybridge::Axis::X, sx);
    commandInput.addAxisDelta(esp32keybridge::Axis::Y, sy);
    bridge.update();
    remainingX -= sx;
    remainingY -= sy;
    delay(MOVE_STEP_MS);
  }
  return actionCanContinue(request);
}

bool rehomePointer(const Request &request)
{
  for (int i = 0; i < HOME_REPORTS; ++i)
  {
    if (!actionCanContinue(request))
    {
      return false;
    }
    commandInput.addAxisDelta(esp32keybridge::Axis::X, -127);
    commandInput.addAxisDelta(esp32keybridge::Axis::Y, -127);
    bridge.update();
    delay(2);
  }
  return actionCanContinue(request);
}

bool sendKey(const Request &request)
{
  using esp32keybridge::KeyboardUsage;
  KeyboardUsage usage = KeyboardUsage::Enter;
  if (request.text == "backspace") usage = KeyboardUsage::Backspace;
  else if (request.text == "escape") usage = KeyboardUsage::Escape;
  else if (request.text == "tab") usage = KeyboardUsage::Tab;

  commandInput.press(usage);
  bridge.update();
  const uint32_t until = millis() + 30;
  while (static_cast<int32_t>(millis() - until) < 0)
  {
    if (!actionCanContinue(request))
    {
      return false;
    }
    delay(1);
  }
  commandInput.release(usage);
  bridge.update();
  return actionCanContinue(request);
}

bool typeText(const Request &request)
{
  for (size_t i = 0; i < request.text.length(); ++i)
  {
    if (!actionCanContinue(request))
    {
      return false;
    }
    const char one[2] = {request.text[i], '\0'};
    bridge.typeText(one);
    do
    {
      if (!actionCanContinue(request))
      {
        return false;
      }
      bridge.update();
      delay(1);
    } while (bridge.typingActive());
  }
  return true;
}

void executeRequest(const Request &request)
{
  if (expired(request))
  {
    sendReply(request.id, request.session, "rejected", "request_expired");
    return;
  }
  if (request.op == "hello")
  {
    if (request.session == activeSession)
    {
      sendReply(request.id, request.session, "rejected", "session_not_new");
      return;
    }
    resetRequestIds();
    rememberRequestId(request.id);
    armSession(request);
    sendReply(request.id, request.session, "executed");
    return;
  }
  if (!matchingSession(request))
  {
    sendReply(request.id, request.session, "rejected", "session_mismatch");
    return;
  }
  if (requestIdSeen(request.id))
  {
    sendReply(request.id, request.session, "rejected", "duplicate_id");
    return;
  }
  rememberRequestId(request.id);
  lastHostActivity = millis();

  if (request.op == "ping")
  {
    sendReply(request.id, request.session, "executed");
    return;
  }
  if (request.op == "stop")
  {
    disarm("stopped", true);
    sendReply(request.id, request.session, "executed");
    return;
  }
  if (!hidOutput.connected())
  {
    sendReply(request.id, request.session, "rejected", "ble_not_ready");
    return;
  }
  const bool mouseOperation = request.op == "move" || request.op == "home" ||
      request.op == "click" || request.op == "press" || request.op == "release" || request.op == "scroll";
  if (mouseOperation && !hidOutput.mouseReady())
  {
    disarm("mouse_not_ready", true);
    sendReply(request.id, request.session, "rejected", "mouse_not_ready");
    return;
  }
  mouseFailureAtActionStart = hidOutput.mouseSendFailureCount();

  cancelRequested = false;
  cancelReason = "cancelled";
  bool succeeded = false;
  if (request.op == "move")
  {
    succeeded = movePaced(request, request.dx, request.dy);
  }
  else if (request.op == "home")
  {
    succeeded = rehomePointer(request);
  }
  else if (request.op == "press")
  {
    if (actionCanContinue(request))
    {
      commandInput.press(esp32keybridge::mouseButtonKey(1));
      bridge.update();
      succeeded = actionCanContinue(request);
    }
  }
  else if (request.op == "release")
  {
    commandInput.release(esp32keybridge::mouseButtonKey(1));
    bridge.update();
    succeeded = actionCanContinue(request);
  }
  else if (request.op == "click")
  {
    if (actionCanContinue(request))
    {
      commandInput.press(esp32keybridge::mouseButtonKey(1));
      bridge.update();
      const uint32_t until = millis() + 60;
      while (static_cast<int32_t>(millis() - until) < 0 && actionCanContinue(request))
      {
        delay(1);
      }
      if (!cancelRequested)
      {
        commandInput.release(esp32keybridge::mouseButtonKey(1));
        bridge.update();
        succeeded = actionCanContinue(request);
      }
    }
  }
  else if (request.op == "scroll")
  {
    commandInput.addAxisDelta(esp32keybridge::Axis::Wheel, request.vertical);
    bridge.update();
    succeeded = actionCanContinue(request);
  }
  else if (request.op == "type")
  {
    succeeded = typeText(request);
  }
  else if (request.op == "key")
  {
    succeeded = sendKey(request);
  }

  if (mouseOperation && hidOutput.mouseSendFailureCount() != mouseFailureAtActionStart)
  {
    disarm("mouse_report_failed", true);
    succeeded = false;
  }

  if (succeeded)
  {
    sendReply(request.id, request.session, "executed");
  }
  else
  {
    releaseAll(true);
    const char *reason = cancelReason;
    if (reason == nullptr || strcmp(reason, "cancelled") == 0)
    {
      reason = expired(request) ? "request_expired" : "action_interrupted";
    }
    sendReply(request.id, request.session, "rejected", reason);
  }
}

} // namespace

void setup()
{
  Serial.begin(115200);
  bridge.addInput(commandInput);
  bridge.addOutput(hidOutput);

  EspBleConfig config;
  config.deviceName = "iPhone Bridge";
  config.security.enabled = true;
  config.security.bonding = true;
  if (!ble.begin(config))
  {
    sendMalformed("ble_init_failed");
    return;
  }
  bleStarted = true;
  ble.advertising().setName(config.deviceName);
  ble.advertising().start();
  ble.onDisconnected([](const EspBleConnection &)
  {
    disarm("ble_disconnected", false);
  });
}

void loop()
{
  if (!bleStarted)
  {
    String line;
    const int lineResult = readLine(line);
    if (lineResult > 0)
    {
      sendMalformed("ble_init_failed");
    }
    return;
  }
  ble.update();
  const bool ready = hidOutput.connected();
  if (lastBleReady && !ready)
  {
    disarm("ble_disconnected", true);
    ble.advertising().start();
  }
  lastBleReady = ready;
  const bool mouseReady = hidOutput.mouseReady();
  if (lastMouseReady && !mouseReady)
  {
    disarm("mouse_not_ready", true);
  }
  lastMouseReady = mouseReady;

  if (!activeSession.isEmpty() && millis() - lastHostActivity > SESSION_WATCHDOG_MS)
  {
    disarm("session_timeout", true);
  }

  String line;
  const int lineResult = readLine(line);
  if (lineResult < 0)
  {
    return;
  }
  if (lineResult == 0 || line.isEmpty())
  {
    return;
  }

  Request request;
  const char *error = nullptr;
  if (!parseRequest(line.c_str(), request, error))
  {
    sendMalformed(error);
    return;
  }
  executeRequest(request);
}
