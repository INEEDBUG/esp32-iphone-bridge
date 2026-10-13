# Public progress

**Updated:** 2026-10-13

**State:** experimental; not a complete device-control release

## Verified so far

- On a single Windows/iPhone 11 iOS 17.5.1 setup, USB screenshot capture and a local synthetic PhoneLab page were exercised.
- Three distinct synthetic test targets have recorded clicks across multiple runs; one vertical scroll was recorded in total. A subsequent image-guard trial clicked a previously tested target again and independently verified its count increase in the new run.
- Image validation can outlast the firmware's 1.5-second session watchdog. Bounded same-session heartbeats now keep that preparation alive; a real UART trace recorded heartbeat acknowledgements and the click acknowledgement, followed by a matching browser event and delayed USB screenshot. Cancellation and the original frame-expiry limit still apply.
- A multi-target trial stopped before any click because the pointer covered the selected label. The planner now uses axis-aligned routes around the label, checked in simulations at two movement gains; real multi-target validation remains pending.
- An explicit image-guarded small-wheel operation is implemented and host-tested. It still needs real-device scroll-result validation.
- The approved board received a serial `hello` session handshake, followed by shared cancellation. The UART trace contained only `hello` and `stop`, with zero HID action writes; release behavior remains unconfirmed. No keyboard key was tested.
- Firmware builds have been checked locally. Build success does not verify radio behavior or iOS response.

## Still open

- The complete 25-target input/scroll matrix and repeatable pointer calibration are not complete.
- Cross-device, cross-version, accessibility, lock-state, drag, and general-purpose text-entry behavior are not established.
- `UNKNOWN` remains the lock-state result when the project cannot verify it. Pairing, screen capture, and browser events do not establish unlock state.
- A full board-backup restoration has not been tested. Experimental firmware must not be treated as recoverable solely because a backup file exists.
- Current host regression passed 415 tests plus 66 subtests, with one skipped Windows symlink-creation test. These are host checks, not additional real-device trials.

## Reproducing a bounded visual experiment

After separately completing the private device binding, approved firmware deployment and synthetic-page preparation, an explicit image-based trial can target a chosen visible probe:

```powershell
.\.venv\Scripts\python.exe -m phonelab.visual_trials --execute --probes 10 --rounds 1 --image-clicks
```

Default invocation observes only. The explicit mode requires fresh real screenshots, a unique detected pointer, the selected target's unchanged image/count, exclusive input ownership and the shared stop generation. A rejected or uncertain action stops the trial without replay. This command does not complete the full acceptance matrix.

No private screenshots, run identifiers, pairing records, firmware images, board backups, tokens, or machine-specific paths are included in this progress note.
