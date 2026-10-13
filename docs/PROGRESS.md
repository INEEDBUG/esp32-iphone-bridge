# Public progress

**Updated:** 2026-10-13

**State:** experimental; not a complete device-control release

## Verified so far

- On a single Windows/iPhone 11 iOS 17.5.1 setup, USB screenshot capture and a local synthetic PhoneLab page were exercised.
- Three synthetic test targets each have one recorded click; one vertical scroll was recorded in total.
- The approved board received a serial `hello` session handshake, followed by shared cancellation. The UART trace contained only `hello` and `stop`, with zero HID action writes; release behavior remains unconfirmed. No keyboard key was tested.
- Firmware builds have been checked locally. Build success does not verify radio behavior or iOS response.

## Still open

- The complete 25-target input/scroll matrix and repeatable pointer calibration are not complete.
- Cross-device, cross-version, accessibility, lock-state, drag, and general-purpose text-entry behavior are not established.
- `UNKNOWN` remains the lock-state result when the project cannot verify it. Pairing, screen capture, and browser events do not establish unlock state.
- A full board-backup restoration has not been tested. Experimental firmware must not be treated as recoverable solely because a backup file exists.
- Current host regression passed: 382 tests plus 66 subtests (448 JUnit cases). These are host checks, not additional real-device trials.

No private screenshots, run identifiers, pairing records, firmware images, board backups, tokens, or machine-specific paths are included in this progress note.
