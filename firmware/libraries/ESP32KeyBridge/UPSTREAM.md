This project-local library copy is from `tanakamasayuki/ESP32KeyBridge` commit
`517adc19f11d1e06d4f4fecc492c7b7e1ebee7e3` and retains its MIT license in
`LICENSE`. Only the core and BLE HID output adapter source files needed for
this firmware are included. Upstream: https://github.com/tanakamasayuki/ESP32KeyBridge

Local safety changes: the BLE output adapter exposes separate mouse-report
readiness and a failed-send counter, plus an explicit method to discard queued
relative motion when a control session stops. Its MIT notice is retained. The
lab sketch drains manual-input axes and discards output axes on stop, new
session, disconnect, expiry, and send failure. Failed reports remain unverified;
an accepted BLE notification does not prove that Safari handled it.
