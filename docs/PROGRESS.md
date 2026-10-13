# Public progress

**Updated:** 2026-10-14

**State:** experimental; not a complete device-control release

## Verified so far

- On one Windows/iPhone 11 iOS 17.5.1 setup, USB screenshot capture and a local synthetic PhoneLab page have been exercised. Historical trials recorded clicks on three distinct synthetic targets across multiple runs and one small vertical scroll. This is a small sample, not a completed matrix.
- The image guard now resolves a shared-edge outline ambiguity with a unique pale-fill region anchored at the OCR label. It accepted the archived synthetic Probe 12 geometry; the same image still failed the final alignment check because its pointer was not aligned. Exact gray masks with bounded morphological cleanup also separated a gray pointer from nearby text in host tests while retaining uniqueness checks.
- A six-move trial in the current run stopped safely without clicking because the cursor occluded the summary. The host correction now preserves the expected total across unreadable frames and advances it only after an independently verified click; this failed trial remains recorded. A fresh real-device trial is pending.
- Text-scope API behavior has host tests only. No real text guard, CLI text action, or text-entry effect has been demonstrated.
- Image validation can outlast the firmware's 1.5-second session watchdog. Bounded same-session heartbeats keep preparation alive; an earlier UART trace recorded heartbeat acknowledgements and a click acknowledgement, followed by a matching browser event and delayed USB screenshot. Cancellation and the original frame-expiry limit still apply.
- The approved board received a serial `hello` session handshake, followed by shared cancellation. The UART trace contained only `hello` and `stop`, with zero HID action writes; release behavior remains unconfirmed. No keyboard key was tested.
- Firmware builds have been checked locally. Build success does not verify radio behavior or iOS response.

## Still open

- The complete 25-target input/scroll matrix, repeatable pointer calibration, and the full visual loop are not complete.
- Real text entry, drag, broader scrolling, reconnect behavior, and sustained input stability remain unverified. The historical click and one-scroll samples do not establish these behaviors.
- Cross-device, cross-version, accessibility, and lock-state behavior are not established. `UNKNOWN` remains the lock-state result when the project cannot verify it; pairing, screen capture, and browser events do not establish unlock state.
- The 22-pixel pointer-disc assumption can be unknown, while the observed lab pointer is about 38 pixels across. Ambiguous or occluded geometry must remain a rejection. The two-second frame-age limit has not been extended.
- Guard validation took about 1.688 seconds in one local run; that measurement is specific to that image and host and is not a performance guarantee.
- Full board-backup restoration has not been tested. Experimental firmware must not be treated as recoverable solely because a backup file exists.
- The reviewed changes passed 453 host tests plus 66 subtests, with one Windows symlink-creation test skipped, in 84.90 seconds. This includes temporary-summary-occlusion, consecutive-click and text-dispatch isolation regressions; it does not establish real-device effects.

## Reproducing a bounded visual experiment

After separately completing the private device binding, approved firmware deployment and synthetic-page preparation, an explicit image-based trial can target a chosen visible probe:

```powershell
.\.venv\Scripts\python.exe -m phonelab.visual_trials --execute --probes 10 --rounds 1 --image-clicks
```

Default invocation observes only. The explicit mode requires fresh real screenshots, a unique detected pointer, the selected target's unchanged image/count, exclusive input ownership and the shared stop generation. A rejected or uncertain action stops the trial without replay. Passing this bounded experiment does not complete the full acceptance matrix.

No private screenshots, run identifiers, pairing records, firmware images, board backups, tokens, or machine-specific paths are included in this progress note.
